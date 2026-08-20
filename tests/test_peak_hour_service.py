# P3.4: peak-hour ranking, built on top of TimeSeriesService.hourly_footfall.
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import EventType
from app.models.event import Event
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession
from app.services.peak_hour_service import PeakHourService


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


def _entry(db: Session, store_id: str, entity_id: str, ts: datetime) -> None:
    if db.get(Store, store_id) is None:
        db.add(Store(id=store_id, name=None))
    if db.get(TrackedEntity, entity_id) is None:
        db.add(TrackedEntity(id=entity_id, store_id=store_id, is_staff=False))
    session = VisitSession(tracked_entity_id=entity_id, store_id=store_id, entry_time=ts)
    db.add(session)
    db.flush()
    db.add(
        Event(
            session_id=session.id,
            tracked_entity_id=entity_id,
            store_id=store_id,
            event_type=EventType.ENTRY,
            timestamp=ts,
        )
    )


def test_peak_hours_ranks_busiest_hour_first(db_session: Session) -> None:
    store_id = "ST_PH1"
    for i in range(3):
        _entry(db_session, store_id, f"V{i}", BASE_TIME + timedelta(hours=1, minutes=i))
    for i in range(3, 5):
        _entry(db_session, store_id, f"V{i}", BASE_TIME + timedelta(minutes=i))
    db_session.commit()

    response = PeakHourService(db_session).peak_hours(store_id, BASE_TIME, BASE_TIME + timedelta(hours=3))

    assert response.peak_hour is not None
    assert response.peak_hour.bucket_start == BASE_TIME + timedelta(hours=1)
    assert response.peak_hour.entries == 3
    assert response.peak_hour.rank == 1
    assert [bucket.rank for bucket in response.ranked_hours] == [1, 2, 3]
    assert [bucket.entries for bucket in response.ranked_hours] == [3, 2, 0]


def test_peak_hours_ties_broken_by_earlier_bucket(db_session: Session) -> None:
    store_id = "ST_PH2"
    _entry(db_session, store_id, "V1", BASE_TIME)
    _entry(db_session, store_id, "V2", BASE_TIME + timedelta(hours=1))
    db_session.commit()

    response = PeakHourService(db_session).peak_hours(store_id, BASE_TIME, BASE_TIME + timedelta(hours=2))

    assert response.peak_hour.bucket_start == BASE_TIME


def test_peak_hours_with_no_traffic_has_no_peak(db_session: Session) -> None:
    db_session.add(Store(id="ST_PH3", name=None))
    db_session.commit()

    response = PeakHourService(db_session).peak_hours("ST_PH3", BASE_TIME, BASE_TIME + timedelta(hours=2))

    assert response.peak_hour is None
    assert [bucket.entries for bucket in response.ranked_hours] == [0, 0]
