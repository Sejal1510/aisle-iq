# P3.5: equal-duration current-vs-previous period comparison, including empty
# periods and zero-denominator percent-change handling.
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import EventType
from app.models.event import Event
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession
from app.services.comparison_service import ComparisonService


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


def test_compare_uses_immediately_preceding_equal_duration_period(db_session: Session) -> None:
    store_id = "ST_CMP1"
    current_start = BASE_TIME + timedelta(hours=2)
    current_end = BASE_TIME + timedelta(hours=4)
    # 2 entries in the current 2-hour window, 1 in the previous 2-hour window
    _entry(db_session, store_id, "V1", current_start + timedelta(minutes=1))
    _entry(db_session, store_id, "V2", current_start + timedelta(minutes=2))
    _entry(db_session, store_id, "V3", BASE_TIME + timedelta(minutes=1))
    db_session.commit()

    response = ComparisonService(db_session).compare(store_id, current_start, current_end)

    assert response.current.start == current_start
    assert response.current.end == current_end
    assert response.previous.start == BASE_TIME
    assert response.previous.end == current_start
    assert response.current.footfall == 2
    assert response.previous.footfall == 1
    assert response.deltas["footfall"].absolute == 1
    assert response.deltas["footfall"].percent == 1.0


def test_compare_handles_zero_previous_denominator_without_crashing(db_session: Session) -> None:
    store_id = "ST_CMP2"
    current_start = BASE_TIME + timedelta(hours=1)
    current_end = BASE_TIME + timedelta(hours=2)
    _entry(db_session, store_id, "V1", current_start + timedelta(minutes=1))
    db_session.commit()

    response = ComparisonService(db_session).compare(store_id, current_start, current_end)

    assert response.previous.footfall == 0
    assert response.current.footfall == 1
    assert response.deltas["footfall"].absolute == 1
    assert response.deltas["footfall"].percent is None


def test_compare_handles_both_periods_empty(db_session: Session) -> None:
    store_id = "ST_CMP3"
    db_session.add(Store(id=store_id, name=None))
    db_session.commit()

    response = ComparisonService(db_session).compare(
        store_id, BASE_TIME + timedelta(hours=1), BASE_TIME + timedelta(hours=2)
    )

    assert response.current.footfall == 0
    assert response.previous.footfall == 0
    assert response.deltas["footfall"].absolute == 0
    assert response.deltas["footfall"].percent is None
    assert response.current.queue_abandonment_rate == 0.0
    assert response.current.average_queue_wait_seconds is None


def test_compare_validates_range(db_session: Session) -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        ComparisonService(db_session).compare("ST_X", BASE_TIME, BASE_TIME)
