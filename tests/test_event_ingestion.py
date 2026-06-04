# PROMPT: Generate tests for event schema validation, ingestion persistence, queue fields, re-entry, and batch idempotency.
# CHANGES MADE: Added canonical event, REENTRY, and partial-failure coverage while preserving sample-event compatibility.
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import EventType, SessionStatus
from app.models.event import Event
from app.models.tracking import TrackedEntity, VisitSession
from app.api.events import ingest_events
from app.schemas.event import (
    EventPayload,
    QueueAbandonedEvent,
    QueueCompletedEvent,
    ZoneEnteredEvent,
    ZoneExitedEvent,
)
from app.services.event_ingestion_service import EventIngestionService
from app.services.analytics_service import AnalyticsService


SAMPLE_EVENTS_PATH = Path(__file__).parent.parent / "data" / "sample_eventsbe42122 (1).jsonl"


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


def load_sample_events() -> list[dict]:
    with SAMPLE_EVENTS_PATH.open("r", encoding="utf-8") as event_file:
        return [json.loads(line) for line in event_file if line.strip()]


def payload_for(event_type: str) -> dict:
    return next(event for event in load_sample_events() if event["event_type"] == event_type)


@pytest.mark.parametrize(
    ("event_type", "expected_type"),
    [
        ("entry", EventType.ENTRY),
        ("exit", EventType.EXIT),
        ("zone_entered", EventType.ZONE_ENTERED),
        ("zone_exited", EventType.ZONE_EXITED),
        ("queue_completed", EventType.QUEUE_COMPLETED),
        ("queue_abandoned", EventType.QUEUE_ABANDONED),
    ],
)
def test_ingests_supported_event_types(
    db_session: Session,
    event_adapter: TypeAdapter,
    event_type: str,
    expected_type: EventType,
) -> None:
    payload = event_adapter.validate_python(payload_for(event_type))
    event = EventIngestionService(db_session).process_event(payload)
    db_session.commit()

    assert event.event_type == expected_type
    assert event.id is not None
    assert event.tracked_entity_id


@pytest.mark.parametrize("event_type", ["zone_entered", "zone_exited"])
def test_zone_events_accept_numeric_track_id(event_adapter: TypeAdapter, event_type: str) -> None:
    payload = event_adapter.validate_python(payload_for(event_type))

    assert isinstance(payload, (ZoneEnteredEvent, ZoneExitedEvent))
    assert payload.track_id in {"101", "102", "103"}


@pytest.mark.parametrize(
    ("event_type", "expected_model", "expected_abandoned"),
    [
        ("queue_completed", QueueCompletedEvent, False),
        ("queue_abandoned", QueueAbandonedEvent, True),
    ],
)
def test_queue_events_accept_numeric_track_id_and_queue_fields(
    event_adapter: TypeAdapter,
    event_type: str,
    expected_model: type,
    expected_abandoned: bool,
) -> None:
    payload = event_adapter.validate_python(payload_for(event_type))

    assert isinstance(payload, expected_model)
    assert payload.track_id in {"101", "102", "103"}
    assert payload.queue_event_id
    assert payload.queue_join_ts is not None
    assert payload.queue_exit_ts is not None
    assert payload.abandoned is expected_abandoned


def test_queue_completed_persists_queue_lifecycle_fields(
    db_session: Session,
    event_adapter: TypeAdapter,
) -> None:
    payload = event_adapter.validate_python(payload_for("queue_completed"))
    event = EventIngestionService(db_session).process_event(payload)
    db_session.commit()

    persisted_event = db_session.get(Event, event.id)

    assert persisted_event is not None
    assert persisted_event.event_type == EventType.QUEUE_COMPLETED
    assert persisted_event.queue_event_id == payload.queue_event_id
    assert persisted_event.queue_join_ts == payload.queue_join_ts
    assert persisted_event.queue_served_ts == payload.queue_served_ts
    assert persisted_event.queue_exit_ts == payload.queue_exit_ts
    assert persisted_event.wait_seconds == payload.wait_seconds
    assert persisted_event.queue_position_at_join == payload.queue_position_at_join
    assert persisted_event.abandoned is False
    assert persisted_event.timestamp == payload.queue_exit_ts


def test_queue_abandoned_persists_queue_lifecycle_fields(
    db_session: Session,
    event_adapter: TypeAdapter,
) -> None:
    payload = event_adapter.validate_python(payload_for("queue_abandoned"))
    event = EventIngestionService(db_session).process_event(payload)
    db_session.commit()

    persisted_event = db_session.get(Event, event.id)

    assert persisted_event is not None
    assert persisted_event.event_type == EventType.QUEUE_ABANDONED
    assert persisted_event.queue_event_id == payload.queue_event_id
    assert persisted_event.queue_served_ts is None
    assert persisted_event.abandoned is True
    assert persisted_event.timestamp == payload.queue_exit_ts


def test_entry_exit_session_lifecycle_remains_backward_compatible(
    db_session: Session,
    event_adapter: TypeAdapter,
) -> None:
    service = EventIngestionService(db_session)
    service.process_event(event_adapter.validate_python(payload_for("entry")))
    exit_event = service.process_event(event_adapter.validate_python(payload_for("exit")))
    db_session.commit()

    session = db_session.get(VisitSession, exit_event.session_id)

    assert session is not None
    assert session.session_status == SessionStatus.COMPLETED
    assert session.exit_time == exit_event.timestamp
    assert session.dwell_seconds == 159


def test_re_entry_creates_new_visit_without_merging_dwell(
    db_session: Session,
    event_adapter: TypeAdapter,
) -> None:
    service = EventIngestionService(db_session)
    first_entry = payload_for("entry")
    first_exit = payload_for("exit")
    second_entry = {**first_entry, "event_timestamp": "2026-04-10T10:10:00"}
    second_exit = {**first_exit, "event_timestamp": "2026-04-10T10:20:00"}

    service.process_event(event_adapter.validate_python(first_entry))
    service.process_event(event_adapter.validate_python(first_exit))
    service.process_event(event_adapter.validate_python(second_entry))
    service.process_event(event_adapter.validate_python(second_exit))
    db_session.commit()

    sessions = list(
        db_session.scalars(
            select(VisitSession)
            .where(VisitSession.tracked_entity_id == first_entry["id_token"])
            .order_by(VisitSession.entry_time)
        )
    )

    assert len(sessions) == 2
    assert all(session.session_status == SessionStatus.COMPLETED for session in sessions)
    assert sessions[0].dwell_seconds == 159
    assert sessions[1].dwell_seconds == 600

    metrics = AnalyticsService(db_session).get_store_metrics(first_entry["store_code"])
    assert metrics.total_visitors == 2
    assert metrics.unique_visitors == 1
    assert db_session.scalar(select(func.count()).select_from(Event).where(Event.event_type == EventType.REENTRY)) == 1


def test_canonical_event_persists_source_id_confidence_and_deduplicates(
    db_session: Session,
    event_adapter: TypeAdapter,
) -> None:
    event_id = str(uuid4())
    raw_event = {
        "event_id": event_id,
        "store_id": "STCANON",
        "camera_id": "CAM_ENTRY",
        "visitor_id": "VIS_1",
        "event_type": "ENTRY",
        "timestamp": "2026-06-01T10:00:00Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.87,
        "metadata": {"queue_depth": None, "session_seq": 1},
    }

    service = EventIngestionService(db_session)
    first = service.process_event(event_adapter.validate_python(raw_event))
    second = service.process_event(event_adapter.validate_python(raw_event))
    db_session.commit()

    assert first.id == second.id
    assert first.source_event_id == event_id
    assert first.confidence == 0.87
    assert db_session.scalar(select(func.count()).select_from(Event)) == 1


def test_batch_ingest_is_idempotent_and_reports_partial_failures(db_session: Session) -> None:
    event_id = str(uuid4())
    valid_event = {
        "event_id": event_id,
        "store_id": "STBATCH",
        "camera_id": "CAM_ENTRY",
        "visitor_id": "VIS_1",
        "event_type": "ENTRY",
        "timestamp": "2026-06-01T10:00:00Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.91,
        "metadata": {"queue_depth": None, "session_seq": 1},
    }

    first = ingest_events([valid_event], db=db_session)
    second = ingest_events([valid_event, {"event_type": "ENTRY"}], db=db_session)

    assert first["accepted"] == 1
    assert second["duplicates"] == 1
    assert second["failed"] == 1
    assert db_session.scalar(select(func.count()).select_from(Event)) == 1


def test_all_sample_events_validate_and_ingest(db_session: Session, event_adapter: TypeAdapter) -> None:
    service = EventIngestionService(db_session)

    for raw_event in load_sample_events():
        payload = event_adapter.validate_python(raw_event)
        service.process_event(payload)

    db_session.commit()

    assert db_session.scalar(select(func.count()).select_from(Event)) == 13
    assert db_session.scalar(select(func.count()).select_from(TrackedEntity)) == 6
    assert db_session.scalar(
        select(func.count()).select_from(Event).where(Event.event_type == EventType.QUEUE_COMPLETED)
    ) == 2
    assert db_session.scalar(
        select(func.count()).select_from(Event).where(Event.event_type == EventType.QUEUE_ABANDONED)
    ) == 1
