# P3.1: current/historical store occupancy derived from canonical Entry/Exit/
# Reentry events, including duplicate/repeated/out-of-order edge cases.
from datetime import datetime, timedelta

import pytest
from pydantic import TypeAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession
from app.schemas.event import EventPayload
from app.services.event_ingestion_service import EventIngestionService
from app.services.occupancy_service import OccupancyService


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
def event_adapter() -> TypeAdapter:
    return TypeAdapter(EventPayload)


BASE_TIME = datetime(2026, 6, 1, 9, 0, 0)


def _entry(id_token: str, *, store: str = "ST1076", camera: str = "CAM_ENTRY_1", ts: datetime, is_staff: bool = False, event_id: str | None = None) -> dict:
    payload = {
        "event_type": "entry",
        "id_token": id_token,
        "store_code": store,
        "camera_id": camera,
        "event_timestamp": ts.isoformat(),
        "is_staff": is_staff,
    }
    if event_id:
        payload["event_id"] = event_id
    return payload


def _exit(id_token: str, *, store: str = "ST1076", camera: str = "CAM_ENTRY_1", ts: datetime, event_id: str | None = None) -> dict:
    payload = {
        "event_type": "exit",
        "id_token": id_token,
        "store_code": store,
        "camera_id": camera,
        "event_timestamp": ts.isoformat(),
    }
    if event_id:
        payload["event_id"] = event_id
    return payload


def test_current_occupancy_counts_in_progress_sessions_only(db_session: Session, event_adapter: TypeAdapter) -> None:
    service = EventIngestionService(db_session)
    service.process_event(event_adapter.validate_python(_entry("V1", ts=BASE_TIME)))
    service.process_event(event_adapter.validate_python(_entry("V2", ts=BASE_TIME + timedelta(minutes=1))))
    service.process_event(event_adapter.validate_python(_exit("V1", ts=BASE_TIME + timedelta(minutes=5))))
    db_session.commit()

    response = OccupancyService(db_session).current_occupancy(
        "ST1076", as_of=BASE_TIME + timedelta(minutes=10)
    )

    assert response.occupancy == 1


def test_repeated_entry_without_exit_does_not_double_count(db_session: Session, event_adapter: TypeAdapter) -> None:
    service = EventIngestionService(db_session)
    service.process_event(event_adapter.validate_python(_entry("V1", ts=BASE_TIME, event_id="e1")))
    service.process_event(event_adapter.validate_python(_entry("V1", ts=BASE_TIME + timedelta(minutes=1), event_id="e2")))
    db_session.commit()

    occupancy = OccupancyService(db_session).occupancy_at("ST1076", BASE_TIME + timedelta(minutes=2))

    assert occupancy == 1
    assert db_session.query(VisitSession).count() == 1


def test_exit_without_entry_does_not_count(db_session: Session, event_adapter: TypeAdapter) -> None:
    """F-style edge case: an EXIT with no prior ENTRY produces a zero-dwell,
    already-COMPLETED session at ingestion (pre-existing behavior) -- it must
    never appear as occupying the store."""
    service = EventIngestionService(db_session)
    service.process_event(event_adapter.validate_python(_exit("GHOST", ts=BASE_TIME)))
    db_session.commit()

    assert OccupancyService(db_session).occupancy_at("ST1076", BASE_TIME) == 0
    assert OccupancyService(db_session).occupancy_at("ST1076", BASE_TIME - timedelta(seconds=1)) == 0


def test_reentry_after_exit_counts_again(db_session: Session, event_adapter: TypeAdapter) -> None:
    service = EventIngestionService(db_session)
    service.process_event(event_adapter.validate_python(_entry("V1", ts=BASE_TIME)))
    service.process_event(event_adapter.validate_python(_exit("V1", ts=BASE_TIME + timedelta(minutes=10))))
    service.process_event(event_adapter.validate_python(_entry("V1", ts=BASE_TIME + timedelta(minutes=20))))
    db_session.commit()

    occupancy_svc = OccupancyService(db_session)
    assert occupancy_svc.occupancy_at("ST1076", BASE_TIME + timedelta(minutes=5)) == 1
    assert occupancy_svc.occupancy_at("ST1076", BASE_TIME + timedelta(minutes=15)) == 0
    assert occupancy_svc.occupancy_at("ST1076", BASE_TIME + timedelta(minutes=25)) == 1
    assert db_session.query(VisitSession).count() == 2


def test_out_of_order_entry_does_not_double_count_via_overlapping_sessions(
    db_session: Session, event_adapter: TypeAdapter
) -> None:
    """Regression for the P3 review finding: EventIngestionService's session
    lookup keys only on session_status == IN_PROGRESS, not on timestamp
    order, so out-of-order delivery can open a second, overlapping
    VisitSession for the same entity -- an ENTRY timestamped *before* an
    EXIT that has already been processed finds no IN_PROGRESS session to
    reuse and opens a new one. This is a pre-existing ingestion
    characteristic (not fixed here, per instructions), but occupancy must
    still count the one real, physical visitor once, not twice, at an
    instant where both overlapping sessions match."""
    service = EventIngestionService(db_session)
    service.process_event(event_adapter.validate_python(_entry("V1", ts=BASE_TIME)))
    service.process_event(event_adapter.validate_python(_exit("V1", ts=BASE_TIME + timedelta(minutes=30))))
    # Arrives after the exit above was already processed, but is timestamped
    # earlier than it -- opens a second, overlapping session.
    service.process_event(event_adapter.validate_python(_entry("V1", ts=BASE_TIME + timedelta(minutes=15))))
    db_session.commit()

    assert db_session.query(VisitSession).count() == 2

    occupancy = OccupancyService(db_session).occupancy_at("ST1076", BASE_TIME + timedelta(minutes=20))

    assert occupancy == 1


def test_duplicate_event_replay_does_not_double_count(db_session: Session, event_adapter: TypeAdapter) -> None:
    service = EventIngestionService(db_session)
    payload = event_adapter.validate_python(_entry("V1", ts=BASE_TIME, event_id="dup-1"))
    service.process_event(payload)
    service.process_event(payload)
    service.process_event(payload)
    db_session.commit()

    assert OccupancyService(db_session).occupancy_at("ST1076", BASE_TIME + timedelta(minutes=1)) == 1


def test_occupancy_is_store_scoped(db_session: Session, event_adapter: TypeAdapter) -> None:
    service = EventIngestionService(db_session)
    service.process_event(event_adapter.validate_python(_entry("V1", store="ST_A", ts=BASE_TIME)))
    service.process_event(event_adapter.validate_python(_entry("V2", store="ST_B", ts=BASE_TIME)))
    service.process_event(event_adapter.validate_python(_entry("V3", store="ST_B", ts=BASE_TIME)))
    db_session.commit()

    occupancy_svc = OccupancyService(db_session)
    assert occupancy_svc.occupancy_at("ST_A", BASE_TIME + timedelta(minutes=1)) == 1
    assert occupancy_svc.occupancy_at("ST_B", BASE_TIME + timedelta(minutes=1)) == 2


def test_multiple_cameras_same_store_scoped_identity_counts_once(db_session: Session, event_adapter: TypeAdapter) -> None:
    """id_token identity is store-scoped, not camera-scoped (P2) -- the same
    token read by two different doors/cameras must resolve to one session."""
    service = EventIngestionService(db_session)
    service.process_event(event_adapter.validate_python(_entry("V1", camera="CAM_ENTRY_1", ts=BASE_TIME)))
    db_session.commit()
    assert OccupancyService(db_session).occupancy_at("ST1076", BASE_TIME + timedelta(minutes=1)) == 1

    service.process_event(event_adapter.validate_python(_exit("V1", camera="CAM_ENTRY_2", ts=BASE_TIME + timedelta(minutes=5))))
    db_session.commit()

    assert OccupancyService(db_session).occupancy_at("ST1076", BASE_TIME + timedelta(minutes=6)) == 0
    assert db_session.query(VisitSession).count() == 1


def test_staff_excluded_from_occupancy(db_session: Session, event_adapter: TypeAdapter) -> None:
    service = EventIngestionService(db_session)
    service.process_event(event_adapter.validate_python(_entry("STAFF1", ts=BASE_TIME, is_staff=True)))
    db_session.commit()

    assert OccupancyService(db_session).occupancy_at("ST1076", BASE_TIME + timedelta(minutes=1)) == 0


def test_occupancy_at_boundaries_are_half_open(db_session: Session) -> None:
    store_id = "ST_BOUND"
    db_session.add(Store(id=store_id, name=None))
    entity = TrackedEntity(store_id=store_id, is_staff=False)
    db_session.add(entity)
    db_session.flush()
    entry_time = BASE_TIME
    exit_time = BASE_TIME + timedelta(minutes=30)
    db_session.add(
        VisitSession(
            tracked_entity_id=entity.id,
            store_id=store_id,
            entry_time=entry_time,
            exit_time=exit_time,
            dwell_seconds=1800,
        )
    )
    db_session.commit()

    occupancy_svc = OccupancyService(db_session)
    assert occupancy_svc.occupancy_at(store_id, entry_time) == 1
    assert occupancy_svc.occupancy_at(store_id, exit_time - timedelta(seconds=1)) == 1
    assert occupancy_svc.occupancy_at(store_id, exit_time) == 0
    assert occupancy_svc.occupancy_at(store_id, entry_time - timedelta(seconds=1)) == 0


def test_occupancy_series_produces_expected_bucket_counts(db_session: Session) -> None:
    store_id = "ST_SERIES"
    db_session.add(Store(id=store_id, name=None))
    entity = TrackedEntity(store_id=store_id, is_staff=False)
    db_session.add(entity)
    db_session.flush()
    db_session.add(
        VisitSession(
            tracked_entity_id=entity.id,
            store_id=store_id,
            entry_time=BASE_TIME,
            exit_time=BASE_TIME + timedelta(minutes=90),
            dwell_seconds=5400,
        )
    )
    db_session.commit()

    series = OccupancyService(db_session).occupancy_series(
        store_id, BASE_TIME, BASE_TIME + timedelta(minutes=120), bucket_minutes=60
    )

    assert [point.occupancy for point in series.points] == [1, 1, 0]
    assert series.points[0].bucket_start == BASE_TIME
    assert series.points[1].bucket_start == BASE_TIME + timedelta(minutes=60)
    assert series.points[2].bucket_start == BASE_TIME + timedelta(minutes=120)


def test_occupancy_series_validates_range(db_session: Session) -> None:
    occupancy_svc = OccupancyService(db_session)
    with pytest.raises(ValueError, match="end must be after start"):
        occupancy_svc.occupancy_series("ST_X", BASE_TIME, BASE_TIME, bucket_minutes=60)
    with pytest.raises(ValueError, match="bucket_minutes must be positive"):
        occupancy_svc.occupancy_series("ST_X", BASE_TIME, BASE_TIME + timedelta(hours=1), bucket_minutes=0)


def test_current_occupancy_as_of_defaults_to_latest_event_timestamp(db_session: Session, event_adapter: TypeAdapter) -> None:
    service = EventIngestionService(db_session)
    service.process_event(event_adapter.validate_python(_entry("V1", ts=BASE_TIME)))
    db_session.commit()

    response = OccupancyService(db_session).current_occupancy("ST1076")

    assert response.as_of == BASE_TIME
    assert response.occupancy == 1


def test_average_occupancy_matches_manual_bucket_average(db_session: Session) -> None:
    store_id = "ST_AVG"
    db_session.add(Store(id=store_id, name=None))
    entity = TrackedEntity(store_id=store_id, is_staff=False)
    db_session.add(entity)
    db_session.flush()
    db_session.add(
        VisitSession(
            tracked_entity_id=entity.id,
            store_id=store_id,
            entry_time=BASE_TIME,
            exit_time=BASE_TIME + timedelta(minutes=60),
            dwell_seconds=3600,
        )
    )
    db_session.commit()

    average = OccupancyService(db_session).average_occupancy(
        store_id, BASE_TIME, BASE_TIME + timedelta(minutes=120), bucket_minutes=60
    )

    # exit_time == 60min bucket boundary is exclusive (half-open interval), so
    # only the t=0 bucket counts this session as occupying the store.
    assert average == round((1 + 0 + 0) / 3, 2)
