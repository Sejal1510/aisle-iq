# P3.3: hourly footfall and queue-activity time series over arbitrary ranges.
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import EventType
from app.models.event import Event
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession
from app.services.time_series_service import TimeSeriesService, bucket_boundaries


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


BASE_TIME = datetime(2026, 6, 1, 8, 0, 0)


def _seed_entity(db: Session, store_id: str, entity_id: str, *, is_staff: bool = False) -> VisitSession:
    if db.get(Store, store_id) is None:
        db.add(Store(id=store_id, name=None))
    db.add(TrackedEntity(id=entity_id, store_id=store_id, is_staff=is_staff))
    session = VisitSession(tracked_entity_id=entity_id, store_id=store_id, entry_time=BASE_TIME)
    db.add(session)
    db.flush()
    return session


def _event(db: Session, session: VisitSession, *, entity_id: str, store_id: str, event_type: EventType, ts: datetime, **kwargs) -> None:
    db.add(
        Event(
            session_id=session.id,
            tracked_entity_id=entity_id,
            store_id=store_id,
            event_type=event_type,
            timestamp=ts,
            **kwargs,
        )
    )


def test_bucket_boundaries_covers_full_range_exclusive_of_end() -> None:
    boundaries = bucket_boundaries(BASE_TIME, BASE_TIME + timedelta(hours=3), bucket_minutes=60)

    assert boundaries == [BASE_TIME, BASE_TIME + timedelta(hours=1), BASE_TIME + timedelta(hours=2)]


def test_bucket_boundaries_handles_uneven_division() -> None:
    boundaries = bucket_boundaries(BASE_TIME, BASE_TIME + timedelta(minutes=90), bucket_minutes=60)

    assert boundaries == [BASE_TIME, BASE_TIME + timedelta(minutes=60)]


def test_bucket_boundaries_validates_range() -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        bucket_boundaries(BASE_TIME, BASE_TIME, bucket_minutes=60)
    with pytest.raises(ValueError, match="bucket_minutes must be positive"):
        bucket_boundaries(BASE_TIME, BASE_TIME + timedelta(hours=1), bucket_minutes=-5)


def test_hourly_footfall_buckets_entries_by_hour(db_session: Session) -> None:
    store_id = "ST_TS1"
    s1 = _seed_entity(db_session, store_id, "V1")
    s2 = _seed_entity(db_session, store_id, "V2")
    s3 = _seed_entity(db_session, store_id, "V3")
    _event(db_session, s1, entity_id="V1", store_id=store_id, event_type=EventType.ENTRY, ts=BASE_TIME + timedelta(minutes=10))
    _event(db_session, s2, entity_id="V2", store_id=store_id, event_type=EventType.ENTRY, ts=BASE_TIME + timedelta(minutes=50))
    _event(db_session, s3, entity_id="V3", store_id=store_id, event_type=EventType.ENTRY, ts=BASE_TIME + timedelta(hours=1, minutes=5))
    db_session.commit()

    response = TimeSeriesService(db_session).hourly_footfall(store_id, BASE_TIME, BASE_TIME + timedelta(hours=2))

    assert [bucket.entries for bucket in response.buckets] == [2, 1]
    assert response.buckets[0].bucket_start == BASE_TIME


def test_hourly_footfall_excludes_staff(db_session: Session) -> None:
    store_id = "ST_TS2"
    staff_session = _seed_entity(db_session, store_id, "STAFF1", is_staff=True)
    _event(db_session, staff_session, entity_id="STAFF1", store_id=store_id, event_type=EventType.ENTRY, ts=BASE_TIME)
    db_session.commit()

    response = TimeSeriesService(db_session).hourly_footfall(store_id, BASE_TIME, BASE_TIME + timedelta(hours=1))

    assert response.buckets[0].entries == 0


def test_hourly_footfall_excludes_events_outside_range(db_session: Session) -> None:
    store_id = "ST_TS3"
    session = _seed_entity(db_session, store_id, "V1")
    _event(db_session, session, entity_id="V1", store_id=store_id, event_type=EventType.ENTRY, ts=BASE_TIME - timedelta(minutes=1))
    _event(db_session, session, entity_id="V1", store_id=store_id, event_type=EventType.EXIT, ts=BASE_TIME + timedelta(minutes=30))
    db_session.commit()

    response = TimeSeriesService(db_session).hourly_footfall(store_id, BASE_TIME, BASE_TIME + timedelta(hours=1))

    # the ENTRY before `start` is excluded; the EXIT is not an ENTRY at all
    assert response.buckets[0].entries == 0


def test_hourly_queue_activity_buckets_join_complete_abandon(db_session: Session) -> None:
    store_id = "ST_TS4"
    session = _seed_entity(db_session, store_id, "V1")
    _event(db_session, session, entity_id="V1", store_id=store_id, event_type=EventType.BILLING_QUEUE_JOIN, ts=BASE_TIME + timedelta(minutes=5), queue_event_id="A")
    _event(db_session, session, entity_id="V1", store_id=store_id, event_type=EventType.QUEUE_COMPLETED, ts=BASE_TIME + timedelta(minutes=10), queue_event_id="A", wait_seconds=300)
    _event(db_session, session, entity_id="V1", store_id=store_id, event_type=EventType.QUEUE_ABANDONED, ts=BASE_TIME + timedelta(hours=1, minutes=5), queue_event_id="B", wait_seconds=100)
    db_session.commit()

    response = TimeSeriesService(db_session).hourly_queue_activity(store_id, BASE_TIME, BASE_TIME + timedelta(hours=2))

    assert response.buckets[0].joined == 1
    assert response.buckets[0].completed == 1
    assert response.buckets[0].abandoned == 0
    assert response.buckets[1].abandoned == 1


def test_hourly_footfall_is_store_scoped(db_session: Session) -> None:
    session_a = _seed_entity(db_session, "ST_TSA", "V1")
    session_b = _seed_entity(db_session, "ST_TSB", "V2")
    _event(db_session, session_a, entity_id="V1", store_id="ST_TSA", event_type=EventType.ENTRY, ts=BASE_TIME)
    _event(db_session, session_b, entity_id="V2", store_id="ST_TSB", event_type=EventType.ENTRY, ts=BASE_TIME)
    _event(db_session, session_b, entity_id="V2", store_id="ST_TSB", event_type=EventType.ENTRY, ts=BASE_TIME)
    db_session.commit()

    service = TimeSeriesService(db_session)
    assert service.hourly_footfall("ST_TSA", BASE_TIME, BASE_TIME + timedelta(hours=1)).buckets[0].entries == 1
    assert service.hourly_footfall("ST_TSB", BASE_TIME, BASE_TIME + timedelta(hours=1)).buckets[0].entries == 2
