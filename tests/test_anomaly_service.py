# P4.3: trend-aware anomaly detection, layered on top of the pre-existing
# static (all-time) rules via ComparisonService. Two test styles are used
# deliberately:
#   - Pure unit tests against hand-built PeriodMetrics for the numeric
#     significance-gate/severity/grouping logic (_footfall_signal etc are
#     pure functions of two PeriodMetrics -- precise boundary values are far
#     easier to construct this way than by engineering exact
#     average_occupancy/wait-time floats out of raw ingested events).
#   - DB-backed integration tests (matching the existing P3 fixture style)
#     for backward compatibility and the real end-to-end entry point
#     (get_store_anomalies -> ComparisonService -> clustering).
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import EventType, SessionStatus
from app.models.event import Event
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession
from app.schemas.live_analytics import PeriodMetrics
from app.services.anomaly_service import AnomalyService

BASE_TIME = datetime(2026, 6, 1, 8, 0, 0)


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def service(db_session: Session) -> AnomalyService:
    return AnomalyService(db_session)


def _period(
    *,
    footfall: int = 0,
    queue_completed: int = 0,
    queue_abandoned: int = 0,
    queue_abandonment_rate: float = 0.0,
    average_queue_wait_seconds: float | None = None,
    average_occupancy: float = 0.0,
    start: datetime = BASE_TIME,
    end: datetime = BASE_TIME + timedelta(days=1),
) -> PeriodMetrics:
    return PeriodMetrics(
        start=start,
        end=end,
        footfall=footfall,
        unique_visitors=footfall,
        queue_joined=queue_completed + queue_abandoned,
        queue_completed=queue_completed,
        queue_abandoned=queue_abandoned,
        queue_abandonment_rate=queue_abandonment_rate,
        average_queue_wait_seconds=average_queue_wait_seconds,
        average_occupancy=average_occupancy,
    )


# ---------------------------------------------------------------------------
# Footfall spike/drop -- significance gates
# ---------------------------------------------------------------------------


def test_footfall_below_percent_gate_does_not_fire(service: AnomalyService) -> None:
    # Real ST1001 P4.1 case: 60 -> 75 is +25%, below the 30% gate.
    previous = _period(footfall=60)
    current = _period(footfall=75)

    assert service._footfall_signal(current, previous) is None


def test_footfall_above_both_gates_fires(service: AnomalyService) -> None:
    # Real ST1002 P4.1 case: 40 -> 53 is +32.5% and +13 visitors.
    previous = _period(footfall=40)
    current = _period(footfall=53)

    signal = service._footfall_signal(current, previous)

    assert signal is not None
    assert signal.direction == "increase"
    assert signal.absolute_change == 13
    assert signal.bad_direction is False


def test_footfall_below_minimum_sample_does_not_fire_even_if_percent_qualifies(service: AnomalyService) -> None:
    # 3 -> 6 is +100%, comfortably past the percent/absolute gates, but both
    # periods are below the minimum-sample floor -- must not fire.
    previous = _period(footfall=3)
    current = _period(footfall=6)

    assert service._footfall_signal(current, previous) is None


def test_footfall_drop_is_bad_direction(service: AnomalyService) -> None:
    previous = _period(footfall=60)
    current = _period(footfall=30)

    signal = service._footfall_signal(current, previous)

    assert signal.direction == "decrease"
    assert signal.bad_direction is True


# ---------------------------------------------------------------------------
# Occupancy -- absolute-gated, not percent-gated (the bug this test guards)
# ---------------------------------------------------------------------------


def test_occupancy_small_absolute_change_does_not_fire_despite_huge_percent(service: AnomalyService) -> None:
    """Real ST1001 P4.1 case: 0.56 -> 0.76 is +0.2 absolute (below the 0.3
    gate) even though the underlying data is real and not noise."""
    previous = _period(average_occupancy=0.56)
    current = _period(average_occupancy=0.76)

    assert service._occupancy_signal(current, previous) is None


def test_occupancy_large_absolute_change_fires_even_from_tiny_baseline(service: AnomalyService) -> None:
    """Real ST1002 P4.1 case: 0.12 -> 0.72 is +500% by percent (would be an
    extreme outlier under a percent-only gate) but a real, moderate +0.6
    absolute change -- must fire via the absolute gate, and percent_change is
    still reported (not hidden), just not used as the significance test."""
    previous = _period(average_occupancy=0.12)
    current = _period(average_occupancy=0.72)

    signal = service._occupancy_signal(current, previous)

    assert signal is not None
    assert signal.absolute_change == 0.6
    assert signal.percent_change == pytest.approx(5.0)
    assert signal.direction == "increase"


def test_occupancy_zero_previous_baseline_does_not_crash(service: AnomalyService) -> None:
    previous = _period(average_occupancy=0.0)
    current = _period(average_occupancy=0.5)

    signal = service._occupancy_signal(current, previous)

    assert signal is not None
    assert signal.percent_change is None  # undefined from a zero baseline, not fabricated


# ---------------------------------------------------------------------------
# Queue abandonment -- increase-only, points-gated, sample-size-guarded
# ---------------------------------------------------------------------------


def test_abandonment_decrease_never_fires_regardless_of_magnitude(service: AnomalyService) -> None:
    # Real ST1001 P4.1 case: rate genuinely improved (28.57% -> 11.9%).
    previous = _period(queue_completed=25, queue_abandoned=10, queue_abandonment_rate=0.2857)
    current = _period(queue_completed=37, queue_abandoned=5, queue_abandonment_rate=0.119)

    assert service._abandonment_signal(current, previous) is None


def test_abandonment_increase_below_points_gate_does_not_fire(service: AnomalyService) -> None:
    previous = _period(queue_completed=20, queue_abandoned=5, queue_abandonment_rate=0.20)
    current = _period(queue_completed=20, queue_abandoned=6, queue_abandonment_rate=0.25)  # +5 points

    assert service._abandonment_signal(current, previous) is None


def test_abandonment_increase_above_points_gate_fires(service: AnomalyService) -> None:
    previous = _period(queue_completed=20, queue_abandoned=5, queue_abandonment_rate=0.20)
    current = _period(queue_completed=15, queue_abandoned=10, queue_abandonment_rate=0.40)  # +20 points

    signal = service._abandonment_signal(current, previous)

    assert signal is not None
    assert signal.direction == "increase"
    assert signal.bad_direction is True
    assert signal.absolute_change == pytest.approx(0.20)


def test_abandonment_below_minimum_sample_does_not_fire(service: AnomalyService) -> None:
    # 1 of 2 abandoned = 50% -- a real percentage but from too small a sample.
    previous = _period(queue_completed=1, queue_abandoned=0, queue_abandonment_rate=0.0)
    current = _period(queue_completed=1, queue_abandoned=1, queue_abandonment_rate=0.50)

    assert service._abandonment_signal(current, previous) is None


# ---------------------------------------------------------------------------
# Wait time -- increase-only, percent+absolute gated, sample-size-guarded
# ---------------------------------------------------------------------------


def test_wait_time_small_change_does_not_fire(service: AnomalyService) -> None:
    # Real ST1002 P4.1 case: 259.0s -> 265.44s (+2.5%).
    previous = _period(queue_completed=15, queue_abandoned=6, average_queue_wait_seconds=259.0)
    current = _period(queue_completed=22, queue_abandoned=5, average_queue_wait_seconds=265.44)

    assert service._wait_time_signal(current, previous) is None


def test_wait_time_large_increase_fires(service: AnomalyService) -> None:
    previous = _period(queue_completed=15, queue_abandoned=6, average_queue_wait_seconds=200.0)
    current = _period(queue_completed=15, queue_abandoned=6, average_queue_wait_seconds=280.0)  # +40%, +80s

    signal = service._wait_time_signal(current, previous)

    assert signal is not None
    assert signal.direction == "increase"
    assert signal.absolute_change == pytest.approx(80.0)


def test_wait_time_decrease_never_fires(service: AnomalyService) -> None:
    previous = _period(queue_completed=15, queue_abandoned=6, average_queue_wait_seconds=280.0)
    current = _period(queue_completed=15, queue_abandoned=6, average_queue_wait_seconds=200.0)

    assert service._wait_time_signal(current, previous) is None


def test_wait_time_below_minimum_sample_does_not_fire(service: AnomalyService) -> None:
    previous = _period(queue_completed=1, queue_abandoned=0, average_queue_wait_seconds=100.0)
    current = _period(queue_completed=1, queue_abandoned=0, average_queue_wait_seconds=500.0)

    assert service._wait_time_signal(current, previous) is None


# ---------------------------------------------------------------------------
# Grouping/clustering -- via the real DB-backed entry point
# ---------------------------------------------------------------------------


def _seed_store(db: Session, store_id: str) -> None:
    if db.get(Store, store_id) is None:
        db.add(Store(id=store_id, name=None))


def _entry(db: Session, store_id: str, entity_id: str, ts: datetime) -> VisitSession:
    _seed_store(db, store_id)
    if db.get(TrackedEntity, entity_id) is None:
        db.add(TrackedEntity(id=entity_id, store_id=store_id, is_staff=False))
    session = VisitSession(tracked_entity_id=entity_id, store_id=store_id, entry_time=ts)
    db.add(session)
    db.flush()
    db.add(
        Event(session_id=session.id, tracked_entity_id=entity_id, store_id=store_id, event_type=EventType.ENTRY, timestamp=ts)
    )
    return session


def _queue_terminal(db: Session, session: VisitSession, *, entity_id: str, store_id: str, ts: datetime, wait_seconds: int, abandoned: bool) -> None:
    db.add(
        Event(
            session_id=session.id,
            tracked_entity_id=entity_id,
            store_id=store_id,
            event_type=EventType.QUEUE_ABANDONED if abandoned else EventType.QUEUE_COMPLETED,
            timestamp=ts,
            queue_event_id=f"{entity_id}-Q",
            wait_seconds=wait_seconds,
            abandoned=abandoned,
        )
    )


def _exit(db: Session, session: VisitSession, *, entity_id: str, store_id: str, ts: datetime) -> None:
    # Every test-constructed session must be explicitly closed. These tests
    # insert Event rows directly rather than going through
    # EventIngestionService, so an EXIT-typed Event row alone does NOT close
    # the session -- session.exit_time/session_status is a side effect
    # EventIngestionService.process_event applies itself (step 7), not
    # something that follows automatically from the Event row existing. An
    # open (IN_PROGRESS) VisitSession counts toward average_occupancy for the
    # rest of the comparison window, which silently turned a queue-only or
    # footfall-only test into an unintended traffic-cluster alert too --
    # caught by test_queue_health_cluster_signals_are_grouped_and_critical
    # and test_different_clusters_are_never_merged both unexpectedly
    # producing a second trend_traffic_increase alert.
    db.add(
        Event(session_id=session.id, tracked_entity_id=entity_id, store_id=store_id, event_type=EventType.EXIT, timestamp=ts)
    )
    session.exit_time = ts
    session.session_status = SessionStatus.COMPLETED
    session.dwell_seconds = int((ts - session.entry_time).total_seconds())


def test_traffic_cluster_signals_are_grouped_into_one_alert(db_session: Session) -> None:
    """Footfall spike + occupancy spike, same direction, same window -> one
    trend_traffic_increase alert, not two separate cards."""
    store_id = "ST_GROUP_TRAFFIC"
    day1 = datetime(2026, 6, 1)
    day2 = datetime(2026, 6, 2)
    day3 = datetime(2026, 6, 3)

    # Day 1: 12 low-occupancy visitors (short visits, no overlap).
    for i in range(12):
        ts = day1 + timedelta(hours=9 + i)
        session = _entry(db_session, store_id, f"V1-{i}", ts)
        _exit(db_session, session, entity_id=f"V1-{i}", store_id=store_id, ts=ts + timedelta(minutes=5))

    # Day 2: 20 visitors with long, overlapping visits (both footfall and
    # occupancy rise together).
    for i in range(20):
        ts = day2 + timedelta(hours=9) + timedelta(minutes=i * 20)
        session = _entry(db_session, store_id, f"V2-{i}", ts)
        _exit(db_session, session, entity_id=f"V2-{i}", store_id=store_id, ts=ts + timedelta(hours=6))
    db_session.commit()

    response = AnomalyService(db_session).get_store_anomalies(store_id, start=day2, end=day3)

    trend_alerts = [a for a in response.anomalies if a.anomaly_type.startswith("trend_")]
    assert len(trend_alerts) == 1
    assert trend_alerts[0].anomaly_type == "trend_traffic_increase"
    assert trend_alerts[0].metric == "footfall"  # fixed cluster preference, not percent-ranked
    assert len(trend_alerts[0].related_signals) == 1
    assert "occupancy" in trend_alerts[0].related_signals[0].lower()
    assert "increased" in trend_alerts[0].related_signals[0].lower()
    # Evidence-aware, non-causal wording only.
    assert "caused" not in trend_alerts[0].message.lower()
    assert "caused" not in trend_alerts[0].related_signals[0].lower()


def test_queue_health_cluster_signals_are_grouped_and_critical(db_session: Session) -> None:
    """Abandonment increase + wait-time increase together -> one grouped
    alert, escalated to CRITICAL (mirrors _queue_spike's own two-tier style)."""
    store_id = "ST_GROUP_QUEUE"
    day1 = datetime(2026, 6, 1)
    day2 = datetime(2026, 6, 2)
    day3 = datetime(2026, 6, 3)

    for i in range(10):
        ts = day1 + timedelta(hours=9 + i % 10)
        session = _entry(db_session, store_id, f"P1-{i}", ts)
        _queue_terminal(db_session, session, entity_id=f"P1-{i}", store_id=store_id, ts=ts + timedelta(minutes=3), wait_seconds=100, abandoned=False)
        _exit(db_session, session, entity_id=f"P1-{i}", store_id=store_id, ts=ts + timedelta(minutes=5))

    for i in range(10):
        ts = day2 + timedelta(hours=9 + i % 10)
        session = _entry(db_session, store_id, f"P2-{i}", ts)
        abandoned = i < 6  # 6 of 10 abandon -- a large increase from ~0%
        _queue_terminal(db_session, session, entity_id=f"P2-{i}", store_id=store_id, ts=ts + timedelta(minutes=8), wait_seconds=300, abandoned=abandoned)
        _exit(db_session, session, entity_id=f"P2-{i}", store_id=store_id, ts=ts + timedelta(minutes=10))
    db_session.commit()

    response = AnomalyService(db_session).get_store_anomalies(store_id, start=day2, end=day3)

    trend_alerts = [a for a in response.anomalies if a.anomaly_type.startswith("trend_")]
    assert len(trend_alerts) == 1
    assert trend_alerts[0].anomaly_type == "trend_queue_health_decline"
    assert trend_alerts[0].severity == "CRITICAL"
    assert trend_alerts[0].metric == "queue_abandonment_rate"
    assert len(trend_alerts[0].related_signals) == 1
    assert "wait time" in trend_alerts[0].related_signals[0].lower()


def test_different_clusters_are_never_merged(db_session: Session) -> None:
    """A footfall spike and a queue-health decline in the same window are two
    separate concerns and must stay as two separate cards."""
    store_id = "ST_NO_MERGE"
    day1 = datetime(2026, 6, 1)
    day2 = datetime(2026, 6, 2)
    day3 = datetime(2026, 6, 3)

    for i in range(10):
        ts = day1 + timedelta(hours=9 + i % 10)
        session = _entry(db_session, store_id, f"P1-{i}", ts)
        _queue_terminal(db_session, session, entity_id=f"P1-{i}", store_id=store_id, ts=ts + timedelta(minutes=3), wait_seconds=100, abandoned=False)
        _exit(db_session, session, entity_id=f"P1-{i}", store_id=store_id, ts=ts + timedelta(minutes=5))
    # Footfall entries are deliberately spread across distinct minute offsets
    # (not aligned to the hour) so brief visits don't coincide with
    # occupancy_series' hourly sample points -- with everyone entering
    # exactly on the hour, enough of these short visits still overlapped an
    # hourly sample to push average_occupancy across its own 0.3 gate too,
    # which defeated this test's purpose (isolating a footfall-only signal).
    for i in range(12):
        ts = day1 + timedelta(hours=9 + i % 11, minutes=(i * 7) % 60)
        session = _entry(db_session, store_id, f"F1-{i}", ts)
        _exit(db_session, session, entity_id=f"F1-{i}", store_id=store_id, ts=ts + timedelta(minutes=5))

    for i in range(10):
        ts = day2 + timedelta(hours=9 + i % 10)
        session = _entry(db_session, store_id, f"P2-{i}", ts)
        abandoned = i < 6
        _queue_terminal(db_session, session, entity_id=f"P2-{i}", store_id=store_id, ts=ts + timedelta(minutes=8), wait_seconds=300, abandoned=abandoned)
        _exit(db_session, session, entity_id=f"P2-{i}", store_id=store_id, ts=ts + timedelta(minutes=10))
    for i in range(25):
        ts = day2 + timedelta(hours=9 + i % 11, minutes=(i * 7) % 60)
        session = _entry(db_session, store_id, f"F2-{i}", ts)
        _exit(db_session, session, entity_id=f"F2-{i}", store_id=store_id, ts=ts + timedelta(minutes=5))
    db_session.commit()

    response = AnomalyService(db_session).get_store_anomalies(store_id, start=day2, end=day3)

    trend_alerts = [a for a in response.anomalies if a.anomaly_type.startswith("trend_")]
    trend_types = {a.anomaly_type for a in trend_alerts}
    assert "trend_footfall_increase" in trend_types
    assert "trend_queue_health_decline" in trend_types
    assert len(trend_alerts) == 2  # not merged into one


# ---------------------------------------------------------------------------
# Backward compatibility -- the hard requirement
# ---------------------------------------------------------------------------


def test_no_range_params_matches_pre_p43_behavior_exactly(db_session: Session) -> None:
    """The exact static rules (queue_spike/conversion_drop/dead_zone), with
    no trend fields populated -- omitting start/end must be indistinguishable
    from calling the pre-P4.3 service."""
    store_id = "ST_BACKCOMPAT"
    _seed_store(db_session, store_id)
    db_session.commit()

    response = AnomalyService(db_session).get_store_anomalies(store_id)

    for anomaly in response.anomalies:
        assert anomaly.anomaly_type in {"queue_spike", "conversion_drop", "dead_zone"}
        assert anomaly.metric is None
        assert anomaly.current_value is None
        assert anomaly.related_signals == []


def test_severity_sort_order_is_preserved(db_session: Session) -> None:
    store_id = "ST_SEVERITY_SORT"
    _seed_store(db_session, store_id)
    db_session.commit()

    response = AnomalyService(db_session).get_store_anomalies(store_id)

    severities = [a.severity for a in response.anomalies]
    order = {"CRITICAL": 0, "WARN": 1, "INFO": 2}
    assert severities == sorted(severities, key=lambda s: order.get(s, 99))
