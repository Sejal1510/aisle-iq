# PROMPT: Generate tests for visitor inference covering staff classification and group clustering heuristics.
# CHANGES MADE: Kept fixtures transparent and asserted confidence/reason fields for explainability.
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import EventType, SessionStatus
from app.models.event import Event
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession
from app.services.visitor_inference_service import VisitorInferenceService


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


def _session(
    db: Session,
    *,
    entity_id: str,
    store_id: str,
    entry_time: datetime,
    dwell_seconds: int,
) -> VisitSession:
    entity = db.get(TrackedEntity, entity_id)
    if entity is None:
        entity = TrackedEntity(id=entity_id, store_id=store_id, is_staff=False)
        db.add(entity)

    session = VisitSession(
        tracked_entity_id=entity_id,
        store_id=store_id,
        entry_time=entry_time,
        exit_time=entry_time + timedelta(seconds=dwell_seconds),
        dwell_seconds=dwell_seconds,
        session_status=SessionStatus.COMPLETED,
    )
    db.add(session)
    db.flush()
    return session


def _event(
    db: Session,
    *,
    session: VisitSession,
    event_type: EventType,
    timestamp: datetime,
    zone_id: str | None = None,
    hotspot_x: float | None = None,
    hotspot_y: float | None = None,
) -> None:
    db.add(
        Event(
            session_id=session.id,
            tracked_entity_id=session.tracked_entity_id,
            store_id=session.store_id,
            camera_id=None,
            zone_id=zone_id,
            event_type=event_type,
            timestamp=timestamp,
            hotspot_x=hotspot_x,
            hotspot_y=hotspot_y,
            is_face_hidden=None,
        )
    )


def test_staff_inference_uses_staff_zone_long_presence_and_billing_behavior(db_session: Session) -> None:
    store_id = "ST1001"
    now = datetime(2026, 6, 1, 10, 0, 0)
    db_session.add(Store(id=store_id, name=None))
    session = _session(db_session, entity_id="staff-track", store_id=store_id, entry_time=now, dwell_seconds=7200)

    _event(db_session, session=session, event_type=EventType.ZONE_ENTERED, timestamp=now, zone_id="ST1001_BOH_STAFF")
    for offset in (5, 15, 30):
        _event(
            db_session,
            session=session,
            event_type=EventType.QUEUE_COMPLETED,
            timestamp=now + timedelta(minutes=offset),
            zone_id="ST1001_BILLING_COUNTER",
        )

    result = VisitorInferenceService(db_session).infer_store(store_id)

    entity = db_session.get(TrackedEntity, "staff-track")
    assert entity is not None
    assert entity.is_staff is True
    assert entity.staff_confidence_score >= 0.65
    assert "known staff zone presence" in (entity.staff_inference_reason or "")
    assert result.staff[0].entity_id == "staff-track"


def test_group_inference_clusters_close_sessions_with_consistent_paths(db_session: Session) -> None:
    store_id = "ST1002"
    now = datetime(2026, 6, 1, 11, 0, 0)
    db_session.add(Store(id=store_id, name=None))
    first = _session(db_session, entity_id="visitor-a", store_id=store_id, entry_time=now, dwell_seconds=900)
    second = _session(
        db_session,
        entity_id="visitor-b",
        store_id=store_id,
        entry_time=now + timedelta(seconds=45),
        dwell_seconds=840,
    )
    solo = _session(
        db_session,
        entity_id="visitor-c",
        store_id=store_id,
        entry_time=now + timedelta(minutes=20),
        dwell_seconds=600,
    )

    for session, x_offset in ((first, 0.0), (second, 0.02)):
        _event(
            db_session,
            session=session,
            event_type=EventType.ZONE_ENTERED,
            timestamp=session.entry_time + timedelta(minutes=1),
            zone_id="MAKEUP",
            hotspot_x=0.40 + x_offset,
            hotspot_y=0.50,
        )
        _event(
            db_session,
            session=session,
            event_type=EventType.ZONE_EXITED,
            timestamp=session.entry_time + timedelta(minutes=4),
            zone_id="MAKEUP",
            hotspot_x=0.42 + x_offset,
            hotspot_y=0.52,
        )

    _event(
        db_session,
        session=solo,
        event_type=EventType.ZONE_ENTERED,
        timestamp=solo.entry_time + timedelta(minutes=1),
        zone_id="FRAGRANCE",
        hotspot_x=0.80,
        hotspot_y=0.20,
    )

    result = VisitorInferenceService(db_session).infer_store(store_id)

    first_entity = db_session.get(TrackedEntity, "visitor-a")
    second_entity = db_session.get(TrackedEntity, "visitor-b")
    solo_entity = db_session.get(TrackedEntity, "visitor-c")
    assert first_entity is not None
    assert second_entity is not None
    assert solo_entity is not None
    assert first_entity.group_id == second_entity.group_id
    assert first_entity.group_size == 2
    assert second_entity.group_size == 2
    assert solo_entity.group_id is None
    assert len(result.groups) == 1


def test_group_inference_preserves_source_group_labels(db_session: Session) -> None:
    store_id = "ST1003"
    now = datetime(2026, 6, 1, 12, 0, 0)
    db_session.add(Store(id=store_id, name=None))
    db_session.add(TrackedEntity(id="source-grouped", store_id=store_id, is_staff=False, group_id="SOURCE-G1", group_size=3))
    db_session.flush()
    _session(db_session, entity_id="source-grouped", store_id=store_id, entry_time=now, dwell_seconds=600)

    VisitorInferenceService(db_session).infer_store(store_id)

    entity = db_session.get(TrackedEntity, "source-grouped")
    assert entity is not None
    assert entity.group_id == "SOURCE-G1"
    assert entity.group_size == 3
