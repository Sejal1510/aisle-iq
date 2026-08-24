# PROMPT: Generate tests for event schema validation, ingestion persistence, queue fields, re-entry, and batch idempotency.
# CHANGES MADE: Added canonical event, REENTRY, and partial-failure coverage while preserving sample-event compatibility.
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.events import ingest_events
from app.core.security import create_api_key
from app.models import Base
from app.models.auth import ApiKey
from app.models.enums import EventType, SessionStatus
from app.models.event import Event
from app.models.store import Store
from app.models.tracking import (
    STORE_SCOPED_CAMERA_ID,
    IdentityAlias,
    TrackedEntity,
    VisitSession,
)
from app.schemas.event import (
    EventPayload,
    QueueAbandonedEvent,
    QueueCompletedEvent,
    ZoneEnteredEvent,
    ZoneExitedEvent,
)
from app.services.analytics_service import AnalyticsService
from app.services.event_ingestion_service import EventIngestionService
from app.services.visitor_inference_service import VisitorInferenceService

SAMPLE_EVENTS_PATH = Path(__file__).parent / "fixtures" / "sample_events.jsonl"


def _make_api_key(db_session: Session, store_id: str) -> ApiKey:
    """Build a real ApiKey for tests that call the ingest_event(s) route
    functions directly (bypassing FastAPI's dependency injection, which would
    otherwise resolve api_key: ApiKey = Depends(require_api_key))."""
    if db_session.get(Store, store_id) is None:
        db_session.add(Store(id=store_id, name=None))
        db_session.flush()
    api_key, _raw_key = create_api_key(db_session, store_id)
    db_session.flush()
    return api_key


def _tracked_entity_id_for(
    db_session: Session,
    *,
    store_id: str,
    source_field: str,
    source_value: str,
    camera_scoped: bool,
    camera_id: str | None = None,
) -> str | None:
    """Resolve a raw source identifier to its current canonical TrackedEntity id
    via the persisted IdentityAlias row, mirroring
    EventIngestionService._resolve_tracked_entity's lookup key."""
    alias_camera_scope = camera_id if camera_scoped else STORE_SCOPED_CAMERA_ID
    alias = db_session.execute(
        select(IdentityAlias).where(
            IdentityAlias.store_id == store_id,
            IdentityAlias.camera_id == alias_camera_scope,
            IdentityAlias.source_field == source_field,
            IdentityAlias.source_value == source_value,
        )
    ).scalar_one_or_none()
    return alias.tracked_entity_id if alias else None


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

    # P2.1: identity resolves through IdentityAlias to a UUID TrackedEntity id,
    # not a bare source id_token -- look the entity up the same way ingestion
    # does rather than assuming an id format.
    canonical_entity_id = _tracked_entity_id_for(
        db_session,
        store_id=first_entry["store_code"],
        source_field="id_token",
        source_value=first_entry["id_token"],
        camera_scoped=False,
    )
    assert canonical_entity_id is not None
    sessions = list(
        db_session.scalars(
            select(VisitSession)
            .where(VisitSession.tracked_entity_id == canonical_entity_id)
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
    api_key = _make_api_key(db_session, "STBATCH")

    first = ingest_events([valid_event], db=db_session, api_key=api_key)
    second = ingest_events([valid_event, {"event_type": "ENTRY"}], db=db_session, api_key=api_key)

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
    # 9, not 6: 3 id_token entities (store-scoped, not camera-scoped) + 3 track_id
    # entities seen on the zone cameras + 3 track_id entities seen on the billing
    # camera. The fixture reuses track_id values 101/102/103 across the zone and
    # billing cameras, but P2.1's identity resolution is camera-scoped for
    # track_id (ByteTrack ids are camera-local, not guaranteed to correlate
    # across cameras) -- so a reused track_id number on a different camera
    # resolves to a different TrackedEntity, not the same one. That is the
    # intended behavior: merging them without evidence would be exactly the
    # unjustified cross-camera identity assumption this phase does not make.
    assert db_session.scalar(select(func.count()).select_from(TrackedEntity)) == 9
    assert db_session.scalar(
        select(func.count()).select_from(Event).where(Event.event_type == EventType.QUEUE_COMPLETED)
    ) == 2
    assert db_session.scalar(
        select(func.count()).select_from(Event).where(Event.event_type == EventType.QUEUE_ABANDONED)
    ) == 1


def test_identity_lookup_is_store_scoped(db_session: Session, event_adapter: TypeAdapter) -> None:
    """F-02 regression: a raw track_id/id_token is never globally unique across
    stores. The same numeric track_id ingested for two different stores must
    produce two distinct TrackedEntity rows, never a merged one."""
    service = EventIngestionService(db_session)
    base_zone_event = payload_for("zone_entered")
    track_id = str(base_zone_event["track_id"])

    store_a_event = {**base_zone_event, "store_id": "ST_IDENTITY_A"}
    store_b_event = {**base_zone_event, "store_id": "ST_IDENTITY_B"}

    service.process_event(event_adapter.validate_python(store_a_event))
    service.process_event(event_adapter.validate_python(store_b_event))
    db_session.commit()

    camera_id = base_zone_event["camera_id"]
    entity_a_id = _tracked_entity_id_for(
        db_session, store_id="ST_IDENTITY_A", source_field="track_id", source_value=track_id,
        camera_scoped=True, camera_id=camera_id,
    )
    entity_b_id = _tracked_entity_id_for(
        db_session, store_id="ST_IDENTITY_B", source_field="track_id", source_value=track_id,
        camera_scoped=True, camera_id=camera_id,
    )
    entity_a = db_session.get(TrackedEntity, entity_a_id) if entity_a_id else None
    entity_b = db_session.get(TrackedEntity, entity_b_id) if entity_b_id else None

    assert entity_a is not None
    assert entity_b is not None
    assert entity_a.id != entity_b.id
    assert entity_a.store_id == "ST_IDENTITY_A"
    assert entity_b.store_id == "ST_IDENTITY_B"
    assert db_session.scalar(
        select(func.count())
        .select_from(TrackedEntity)
        .where(TrackedEntity.store_id.in_(["ST_IDENTITY_A", "ST_IDENTITY_B"]))
    ) == 2


def test_identity_lookup_reuses_entity_within_same_store(db_session: Session, event_adapter: TypeAdapter) -> None:
    """Same-store behavior must remain correct: repeated events for the same
    store_id + track_id reuse one TrackedEntity, not one per event."""
    service = EventIngestionService(db_session)
    zone_entered = payload_for("zone_entered")
    zone_exited = payload_for("zone_exited")
    track_id = zone_entered["track_id"]

    same_store_entered = {**zone_entered, "store_id": "ST_IDENTITY_SAME", "track_id": track_id}
    same_store_exited = {
        **zone_exited,
        "store_id": "ST_IDENTITY_SAME",
        "track_id": track_id,
        "event_time": "2026-05-01T09:05:00",
    }

    service.process_event(event_adapter.validate_python(same_store_entered))
    service.process_event(event_adapter.validate_python(same_store_exited))
    db_session.commit()

    assert db_session.scalar(
        select(func.count()).select_from(TrackedEntity).where(TrackedEntity.store_id == "ST_IDENTITY_SAME")
    ) == 1


def test_batch_does_not_rescan_full_history_per_event(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-04 regression: batch ingestion must run staff/group inference once per
    touched store after the whole batch, not once per event inside it."""
    call_count = {"n": 0}
    original_infer_store = VisitorInferenceService.infer_store

    def counting_infer_store(self, store_id):
        call_count["n"] += 1
        return original_infer_store(self, store_id)

    monkeypatch.setattr(VisitorInferenceService, "infer_store", counting_infer_store)

    events = [
        {
            "event_id": str(uuid4()),
            "store_id": "ST_BATCH_PERF",
            "camera_id": "CAM_ENTRY",
            "visitor_id": f"VIS_{index}",
            "event_type": "ENTRY",
            "timestamp": f"2026-06-01T10:{index:02d}:00Z",
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.9,
            "metadata": {"queue_depth": None, "session_seq": 1},
        }
        for index in range(10)
    ]
    api_key = _make_api_key(db_session, "ST_BATCH_PERF")

    result = ingest_events(events, db=db_session, api_key=api_key)

    assert result["accepted"] == 10
    assert call_count["n"] == 1
