# P3.2: live and period queue intelligence, correlated by queue_event_id,
# including inconsistent/out-of-order queue event sequences.
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import EventType
from app.models.event import Event
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession
from app.services.queue_service import QueueService


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


BASE_TIME = datetime(2026, 6, 1, 9, 0, 0)


def _entity_session(db: Session, store_id: str, entity_id: str) -> VisitSession:
    if db.get(Store, store_id) is None:
        db.add(Store(id=store_id, name=None))
    entity = TrackedEntity(id=entity_id, store_id=store_id, is_staff=False)
    db.add(entity)
    session = VisitSession(tracked_entity_id=entity_id, store_id=store_id, entry_time=BASE_TIME)
    db.add(session)
    db.flush()
    return session


def _join(db: Session, session: VisitSession, *, entity_id: str, store_id: str, queue_event_id: str, ts: datetime) -> None:
    db.add(
        Event(
            session_id=session.id,
            tracked_entity_id=entity_id,
            store_id=store_id,
            event_type=EventType.BILLING_QUEUE_JOIN,
            timestamp=ts,
            queue_event_id=queue_event_id,
        )
    )


def _terminal(
    db: Session,
    session: VisitSession,
    *,
    entity_id: str,
    store_id: str,
    queue_event_id: str,
    ts: datetime,
    event_type: EventType,
    wait_seconds: int | None,
) -> None:
    db.add(
        Event(
            session_id=session.id,
            tracked_entity_id=entity_id,
            store_id=store_id,
            event_type=event_type,
            timestamp=ts,
            queue_event_id=queue_event_id,
            wait_seconds=wait_seconds,
            abandoned=event_type == EventType.QUEUE_ABANDONED,
        )
    )


def test_current_queue_length_and_entities(db_session: Session) -> None:
    store_id = "ST_Q1"
    s1 = _entity_session(db_session, store_id, "V1")
    s2 = _entity_session(db_session, store_id, "V2")
    _join(db_session, s1, entity_id="V1", store_id=store_id, queue_event_id="Q1", ts=BASE_TIME)
    _join(db_session, s2, entity_id="V2", store_id=store_id, queue_event_id="Q2", ts=BASE_TIME + timedelta(minutes=1))
    _terminal(
        db_session, s1, entity_id="V1", store_id=store_id, queue_event_id="Q1",
        ts=BASE_TIME + timedelta(minutes=5), event_type=EventType.QUEUE_COMPLETED, wait_seconds=300,
    )
    db_session.commit()

    response = QueueService(db_session).current_queue(store_id, as_of=BASE_TIME + timedelta(minutes=10))

    assert response.queue_length == 1
    assert [entity.tracked_entity_id for entity in response.queued_entities] == ["V2"]
    assert response.queued_entities[0].waiting_seconds == 9 * 60


def test_join_join_by_same_entity_are_two_independent_visits(db_session: Session) -> None:
    store_id = "ST_Q2"
    session = _entity_session(db_session, store_id, "V1")
    _join(db_session, session, entity_id="V1", store_id=store_id, queue_event_id="A", ts=BASE_TIME)
    _join(db_session, session, entity_id="V1", store_id=store_id, queue_event_id="B", ts=BASE_TIME + timedelta(minutes=1))
    db_session.commit()

    response = QueueService(db_session).current_queue(store_id, as_of=BASE_TIME + timedelta(minutes=5))

    assert response.queue_length == 2


def test_duplicate_join_for_same_queue_event_id_does_not_inflate_queue_length(db_session: Session) -> None:
    """P3 review regression: two JOIN Event rows for the SAME queue_event_id
    (an anomalous duplicate/near-duplicate submission, not a second visit)
    describe one queue visit and must appear once in current_queue, using
    the earliest observed join time -- unlike the join-A/join-B case above,
    which is genuinely two visits and must remain two entries."""
    store_id = "ST_Q2B"
    session = _entity_session(db_session, store_id, "V1")
    _join(db_session, session, entity_id="V1", store_id=store_id, queue_event_id="A", ts=BASE_TIME)
    _join(db_session, session, entity_id="V1", store_id=store_id, queue_event_id="A", ts=BASE_TIME + timedelta(minutes=1))
    db_session.commit()

    response = QueueService(db_session).current_queue(store_id, as_of=BASE_TIME + timedelta(minutes=5))

    assert response.queue_length == 1
    assert response.queued_entities[0].joined_at == BASE_TIME
    assert response.queued_entities[0].waiting_seconds == 5 * 60


def test_duplicate_join_then_completion_still_closes_the_visit(db_session: Session) -> None:
    """The dedup-by-queue_event_id grouping must not prevent the visit from
    later closing normally once a terminal event for it arrives."""
    store_id = "ST_Q2C"
    session = _entity_session(db_session, store_id, "V1")
    _join(db_session, session, entity_id="V1", store_id=store_id, queue_event_id="A", ts=BASE_TIME)
    _join(db_session, session, entity_id="V1", store_id=store_id, queue_event_id="A", ts=BASE_TIME + timedelta(minutes=1))
    _terminal(
        db_session, session, entity_id="V1", store_id=store_id, queue_event_id="A",
        ts=BASE_TIME + timedelta(minutes=5), event_type=EventType.QUEUE_COMPLETED, wait_seconds=300,
    )
    db_session.commit()

    response = QueueService(db_session).current_queue(store_id, as_of=BASE_TIME + timedelta(minutes=10))

    assert response.queue_length == 0


def test_completion_without_join_counts_in_metrics_but_never_queued(db_session: Session) -> None:
    store_id = "ST_Q3"
    session = _entity_session(db_session, store_id, "V1")
    _terminal(
        db_session, session, entity_id="V1", store_id=store_id, queue_event_id="ORPHAN-COMPLETE",
        ts=BASE_TIME, event_type=EventType.QUEUE_COMPLETED, wait_seconds=120,
    )
    db_session.commit()

    queue_svc = QueueService(db_session)
    current = queue_svc.current_queue(store_id, as_of=BASE_TIME - timedelta(minutes=1))
    metrics = queue_svc.queue_metrics(store_id, BASE_TIME - timedelta(hours=1), BASE_TIME + timedelta(hours=1))

    assert current.queue_length == 0
    assert metrics.completed_visits == 1
    assert metrics.average_wait_seconds == 120.0


def test_abandonment_without_join_counts_in_metrics(db_session: Session) -> None:
    store_id = "ST_Q4"
    session = _entity_session(db_session, store_id, "V1")
    _terminal(
        db_session, session, entity_id="V1", store_id=store_id, queue_event_id="ORPHAN-ABANDON",
        ts=BASE_TIME, event_type=EventType.QUEUE_ABANDONED, wait_seconds=200,
    )
    db_session.commit()

    metrics = QueueService(db_session).queue_metrics(store_id, BASE_TIME - timedelta(hours=1), BASE_TIME + timedelta(hours=1))

    assert metrics.abandoned_visits == 1
    assert metrics.completed_visits == 0
    assert metrics.abandonment_rate == 1.0


def test_duplicate_completion_counts_in_both_buckets_without_cross_dedup(db_session: Session) -> None:
    """Two terminal events for the same queue_event_id (a genuinely
    inconsistent replay/data issue) are each counted in their own bucket
    rather than silently merged or dropped -- this is the documented,
    explicit behavior for inconsistent event sequences, not a bug."""
    store_id = "ST_Q5"
    session = _entity_session(db_session, store_id, "V1")
    _terminal(
        db_session, session, entity_id="V1", store_id=store_id, queue_event_id="Q-DUP",
        ts=BASE_TIME, event_type=EventType.QUEUE_COMPLETED, wait_seconds=100,
    )
    _terminal(
        db_session, session, entity_id="V1", store_id=store_id, queue_event_id="Q-DUP",
        ts=BASE_TIME + timedelta(minutes=1), event_type=EventType.QUEUE_ABANDONED, wait_seconds=160,
    )
    db_session.commit()

    metrics = QueueService(db_session).queue_metrics(store_id, BASE_TIME - timedelta(hours=1), BASE_TIME + timedelta(hours=1))

    assert metrics.completed_visits == 1
    assert metrics.abandoned_visits == 1


def test_join_complete_then_another_join(db_session: Session) -> None:
    store_id = "ST_Q6"
    session = _entity_session(db_session, store_id, "V1")
    _join(db_session, session, entity_id="V1", store_id=store_id, queue_event_id="A", ts=BASE_TIME)
    _terminal(
        db_session, session, entity_id="V1", store_id=store_id, queue_event_id="A",
        ts=BASE_TIME + timedelta(minutes=5), event_type=EventType.QUEUE_COMPLETED, wait_seconds=300,
    )
    _join(db_session, session, entity_id="V1", store_id=store_id, queue_event_id="B", ts=BASE_TIME + timedelta(minutes=10))
    db_session.commit()

    current = QueueService(db_session).current_queue(store_id, as_of=BASE_TIME + timedelta(minutes=15))

    assert current.queue_length == 1
    assert current.queued_entities[0].queue_event_id == "B"


def test_average_and_median_wait_seconds(db_session: Session) -> None:
    store_id = "ST_Q7"
    session = _entity_session(db_session, store_id, "V1")
    for index, wait in enumerate([100, 200, 600]):
        _terminal(
            db_session, session, entity_id="V1", store_id=store_id, queue_event_id=f"Q{index}",
            ts=BASE_TIME + timedelta(minutes=index), event_type=EventType.QUEUE_COMPLETED, wait_seconds=wait,
        )
    db_session.commit()

    metrics = QueueService(db_session).queue_metrics(store_id, BASE_TIME - timedelta(hours=1), BASE_TIME + timedelta(hours=1))

    assert metrics.average_wait_seconds == 300.0
    assert metrics.median_wait_seconds == 200.0


def test_queue_metrics_with_no_terminal_events_returns_none_averages(db_session: Session) -> None:
    store_id = "ST_Q8"
    _entity_session(db_session, store_id, "V1")
    db_session.commit()

    metrics = QueueService(db_session).queue_metrics(store_id, BASE_TIME - timedelta(hours=1), BASE_TIME + timedelta(hours=1))

    assert metrics.completed_visits == 0
    assert metrics.abandoned_visits == 0
    assert metrics.abandonment_rate == 0.0
    assert metrics.average_wait_seconds is None
    assert metrics.median_wait_seconds is None


def test_queue_metrics_time_range_excludes_events_outside_window(db_session: Session) -> None:
    store_id = "ST_Q9"
    session = _entity_session(db_session, store_id, "V1")
    _terminal(
        db_session, session, entity_id="V1", store_id=store_id, queue_event_id="OUTSIDE",
        ts=BASE_TIME - timedelta(days=1), event_type=EventType.QUEUE_COMPLETED, wait_seconds=100,
    )
    _terminal(
        db_session, session, entity_id="V1", store_id=store_id, queue_event_id="INSIDE",
        ts=BASE_TIME, event_type=EventType.QUEUE_COMPLETED, wait_seconds=200,
    )
    db_session.commit()

    metrics = QueueService(db_session).queue_metrics(store_id, BASE_TIME - timedelta(hours=1), BASE_TIME + timedelta(hours=1))

    assert metrics.completed_visits == 1
    assert metrics.average_wait_seconds == 200.0


def test_queue_metrics_validates_range(db_session: Session) -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        QueueService(db_session).queue_metrics("ST_X", BASE_TIME, BASE_TIME)


def test_queue_is_store_scoped(db_session: Session) -> None:
    session_a = _entity_session(db_session, "ST_QA", "V1")
    session_b = _entity_session(db_session, "ST_QB", "V2")
    _join(db_session, session_a, entity_id="V1", store_id="ST_QA", queue_event_id="A", ts=BASE_TIME)
    _join(db_session, session_b, entity_id="V2", store_id="ST_QB", queue_event_id="B", ts=BASE_TIME)
    db_session.commit()

    queue_svc = QueueService(db_session)
    assert queue_svc.current_queue("ST_QA", as_of=BASE_TIME + timedelta(minutes=1)).queue_length == 1
    assert queue_svc.current_queue("ST_QB", as_of=BASE_TIME + timedelta(minutes=1)).queue_length == 1
