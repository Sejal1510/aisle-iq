# PROMPT: Regression coverage for P2's data model foundation -- canonical
# identity (UUID + IdentityAlias), Organization/Store/Camera/Zone reference
# data provisioning, RawEvent capture, and store-scoped idempotency for both
# CCTV events and POS transactions.
import json
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import ZoneType
from app.models.event import Event
from app.models.pos import PosTransaction
from app.models.raw_event import RawEvent
from app.models.store import Camera, Organization, Store, Zone
from app.models.tracking import STORE_SCOPED_CAMERA_ID, IdentityAlias, TrackedEntity
from app.schemas.event import EventPayload
from app.services.event_ingestion_service import EventIngestionService
from app.services.pos_ingestion_service import PosIngestionService
from app.services.reference_data_service import (
    DEFAULT_ORGANIZATION_ID,
    ReferenceDataService,
)


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
def fk_enforced_db_session() -> Session:
    """A session whose connection has SQLite foreign key enforcement turned
    on -- the app does not do this itself (see app/db/session.py), but the
    schema's constraints should still be genuinely correct, not just
    accidentally unenforced."""
    engine = create_engine("sqlite:///:memory:")

    @__import__("sqlalchemy").event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

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


SAMPLE_EVENTS_PATH = Path(__file__).parent / "fixtures" / "sample_events.jsonl"


def _load_sample_event(event_type: str) -> dict:
    with SAMPLE_EVENTS_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            event = json.loads(line)
            if event["event_type"] == event_type:
                return event
    raise AssertionError(f"no {event_type} event in fixture")


# ---------------------------------------------------------------------------
# P2.1 -- canonical identity
# ---------------------------------------------------------------------------


def test_tracked_entity_id_is_a_platform_generated_uuid(db_session: Session, event_adapter: TypeAdapter) -> None:
    payload = event_adapter.validate_python(_load_sample_event("entry"))
    event = EventIngestionService(db_session).process_event(payload)
    db_session.commit()

    entity = db_session.get(TrackedEntity, event.tracked_entity_id)
    assert entity is not None
    # Raises ValueError if not a well-formed UUID -- the assertion is that this
    # does not raise, i.e. the id is a real UUID and not "<store>:<id_token>".
    uuid.UUID(entity.id)
    assert not entity.id.startswith(payload.store_code)


def test_identity_alias_created_on_first_sighting_and_reused_on_second(
    db_session: Session, event_adapter: TypeAdapter
) -> None:
    zone_entered = _load_sample_event("zone_entered")
    zone_exited = _load_sample_event("zone_exited")
    assert zone_entered["track_id"] == zone_exited["track_id"]
    assert zone_entered["camera_id"] == zone_exited["camera_id"]

    service = EventIngestionService(db_session)
    first_event = service.process_event(event_adapter.validate_python(zone_entered))
    second_event = service.process_event(event_adapter.validate_python(zone_exited))
    db_session.commit()

    assert first_event.tracked_entity_id == second_event.tracked_entity_id

    aliases = db_session.scalars(
        select(IdentityAlias).where(
            IdentityAlias.store_id == zone_entered["store_id"],
            IdentityAlias.camera_id == zone_entered["camera_id"],
            IdentityAlias.source_field == "track_id",
            IdentityAlias.source_value == str(zone_entered["track_id"]),
        )
    ).all()

    assert len(aliases) == 1
    assert aliases[0].tracked_entity_id == first_event.tracked_entity_id
    assert aliases[0].first_seen_at < aliases[0].last_seen_at or aliases[0].first_seen_at == aliases[0].last_seen_at


def test_id_token_alias_is_store_scoped_not_camera_scoped(db_session: Session, event_adapter: TypeAdapter) -> None:
    """id_token identifiers (entry/exit) are stable across a store's whole
    entry system, unlike track_id -- their alias uses the STORE_SCOPED_CAMERA_ID
    sentinel rather than a real camera_id."""
    payload = event_adapter.validate_python(_load_sample_event("entry"))
    EventIngestionService(db_session).process_event(payload)
    db_session.commit()

    alias = db_session.execute(
        select(IdentityAlias).where(
            IdentityAlias.store_id == payload.store_code,
            IdentityAlias.source_field == "id_token",
            IdentityAlias.source_value == payload.id_token,
        )
    ).scalar_one()

    assert alias.camera_id == STORE_SCOPED_CAMERA_ID


def test_identity_alias_uniqueness_is_enforced_at_the_database_level(fk_enforced_db_session: Session) -> None:
    db_session = fk_enforced_db_session
    entity = TrackedEntity(store_id="ST_UNIQ")
    db_session.add(entity)
    db_session.add(Store(id="ST_UNIQ", name=None))
    db_session.flush()

    kwargs = {
        "tracked_entity_id": entity.id,
        "store_id": "ST_UNIQ",
        "camera_id": "CAM_1",
        "source_field": "track_id",
        "source_value": "101",
        "first_seen_at": datetime(2026, 1, 1),
        "last_seen_at": datetime(2026, 1, 1),
    }
    db_session.add(IdentityAlias(**kwargs))
    db_session.flush()

    db_session.add(IdentityAlias(**{**kwargs, "first_seen_at": datetime(2026, 1, 2)}))
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------------------------------------------------------------------------
# P2.2 -- Organization / Store / Camera / Zone
# ---------------------------------------------------------------------------


def test_ensure_store_provisions_default_organization(db_session: Session) -> None:
    reference_data = ReferenceDataService(db_session)

    store = reference_data.ensure_store("ST_PROVISION")
    db_session.commit()

    assert store.organization_id == DEFAULT_ORGANIZATION_ID
    assert db_session.get(Organization, DEFAULT_ORGANIZATION_ID) is not None
    assert db_session.scalar(select(func.count()).select_from(Organization)) == 1


def test_ensure_store_is_idempotent(db_session: Session) -> None:
    reference_data = ReferenceDataService(db_session)
    reference_data.ensure_store("ST_IDEMPOTENT")
    reference_data.ensure_store("ST_IDEMPOTENT")
    db_session.commit()

    assert db_session.scalar(
        select(func.count()).select_from(Store).where(Store.id == "ST_IDEMPOTENT")
    ) == 1


def test_ensure_camera_auto_creates_store_and_is_idempotent(db_session: Session) -> None:
    reference_data = ReferenceDataService(db_session)

    reference_data.ensure_camera("ST_CAM", "CAM_1", role="zone")
    reference_data.ensure_camera("ST_CAM", "CAM_1", role="zone")
    db_session.commit()

    assert db_session.get(Store, "ST_CAM") is not None
    assert db_session.scalar(select(func.count()).select_from(Camera).where(Camera.id == "CAM_1")) == 1


def test_ensure_zone_coerces_known_and_unknown_zone_types(db_session: Session) -> None:
    reference_data = ReferenceDataService(db_session)

    known = reference_data.ensure_zone("ST_ZONE", "ZONE_SHELF", zone_type="SHELF")
    # "BILLING" is what the video pipeline emits for queue zones, but it is not
    # a ZoneType enum member (CHECKOUT is) -- must fall back to OTHER instead
    # of raising, since this is a labeling mismatch, not an ingestion failure.
    unknown = reference_data.ensure_zone("ST_ZONE", "ZONE_BILLING", zone_type="BILLING")
    db_session.commit()

    assert known.type == ZoneType.SHELF
    assert unknown.type == ZoneType.OTHER


def test_ensure_camera_and_zone_return_none_for_missing_id(db_session: Session) -> None:
    reference_data = ReferenceDataService(db_session)

    assert reference_data.ensure_camera("ST_NONE", None) is None
    assert reference_data.ensure_zone("ST_NONE", None) is None


def test_event_foreign_keys_are_genuinely_enforced(fk_enforced_db_session: Session) -> None:
    """The app relies on auto-provisioning (ReferenceDataService) rather than
    ever hitting this, but the constraint itself must be real: SQLite does not
    enforce foreign keys by default, and PostgreSQL always does, so the schema
    must not be silently relying on that non-enforcement."""
    db_session = fk_enforced_db_session
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO camera (id, store_id, is_active, created_at) "
                "VALUES ('CAM_ORPHAN', 'ST_DOES_NOT_EXIST', 1, :now)"
            ),
            {"now": datetime(2026, 1, 1)},
        )
        db_session.flush()


def test_cctv_ingestion_provisions_store_camera_and_zone(db_session: Session, event_adapter: TypeAdapter) -> None:
    payload = event_adapter.validate_python(_load_sample_event("zone_entered"))
    EventIngestionService(db_session).process_event(payload)
    db_session.commit()

    assert db_session.get(Store, payload.store_id) is not None
    assert db_session.get(Camera, payload.camera_id) is not None
    assert db_session.get(Zone, payload.zone_id) is not None


# ---------------------------------------------------------------------------
# P2.3 -- raw event archive
# ---------------------------------------------------------------------------


def test_raw_event_persisted_alongside_canonical_event(db_session: Session, event_adapter: TypeAdapter) -> None:
    raw_payload = _load_sample_event("entry")
    payload = event_adapter.validate_python(raw_payload)
    event = EventIngestionService(db_session).process_event(payload)
    db_session.commit()

    raw_events = db_session.scalars(select(RawEvent).where(RawEvent.event_id == event.id)).all()

    assert len(raw_events) == 1
    assert raw_events[0].validation_status == "accepted"
    assert raw_events[0].source == "EntryEvent"
    stored_payload = json.loads(raw_events[0].payload_json)
    assert stored_payload["id_token"] == raw_payload["id_token"]
    assert stored_payload["store_code"] == raw_payload["store_code"]


def test_raw_event_recorded_for_duplicate_submission_without_a_second_canonical_event(
    db_session: Session, event_adapter: TypeAdapter
) -> None:
    payload = event_adapter.validate_python(_load_sample_event("entry"))
    service = EventIngestionService(db_session)

    first = service.process_event(payload)
    second = service.process_event(payload)
    db_session.commit()

    assert first.id == second.id
    assert db_session.scalar(select(func.count()).select_from(Event)) == 1
    assert db_session.scalar(select(func.count()).select_from(RawEvent)) == 2

    statuses = sorted(
        row for row in db_session.scalars(select(RawEvent.validation_status))
    )
    assert statuses == ["accepted", "duplicate"]


# ---------------------------------------------------------------------------
# P2.4 -- idempotent ingestion, store-scoped
# ---------------------------------------------------------------------------


def test_first_submission_is_accepted(db_session: Session, event_adapter: TypeAdapter) -> None:
    payload = event_adapter.validate_python(_load_sample_event("entry"))
    event = EventIngestionService(db_session).process_event(payload)
    db_session.commit()

    assert db_session.get(Event, event.id) is not None


def test_identical_replay_does_not_duplicate(db_session: Session, event_adapter: TypeAdapter) -> None:
    payload = event_adapter.validate_python(_load_sample_event("entry"))
    service = EventIngestionService(db_session)

    first = service.process_event(payload)
    second = service.process_event(payload)
    third = service.process_event(payload)
    db_session.commit()

    assert first.id == second.id == third.id
    assert db_session.scalar(select(func.count()).select_from(Event)) == 1


def test_same_source_event_id_in_different_stores_is_allowed(
    db_session: Session, event_adapter: TypeAdapter
) -> None:
    """P2.4 requirement: the idempotency key is (store_id, source_event_id),
    not source_event_id alone -- two different stores' CCTV systems can
    legitimately both produce an event with source id 'evt-1'."""
    shared_event_id = "evt-shared-across-stores"
    canonical_event = {
        "event_id": shared_event_id,
        "store_id": "ST_DUP_A",
        "camera_id": "CAM_ENTRY",
        "visitor_id": "VIS_A",
        "event_type": "ENTRY",
        "timestamp": "2026-06-01T10:00:00Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.9,
        "metadata": {"queue_depth": None, "session_seq": 1},
    }
    other_store_event = {**canonical_event, "store_id": "ST_DUP_B", "visitor_id": "VIS_B"}

    service = EventIngestionService(db_session)
    event_a = service.process_event(event_adapter.validate_python(canonical_event))
    event_b = service.process_event(event_adapter.validate_python(other_store_event))
    db_session.commit()

    assert event_a.id != event_b.id
    assert event_a.store_id == "ST_DUP_A"
    assert event_b.store_id == "ST_DUP_B"
    assert db_session.scalar(
        select(func.count()).select_from(Event).where(Event.source_event_id == shared_event_id)
    ) == 2


def test_genuinely_different_events_are_both_stored(db_session: Session, event_adapter: TypeAdapter) -> None:
    service = EventIngestionService(db_session)
    entry = service.process_event(event_adapter.validate_python(_load_sample_event("entry")))
    exit_ = service.process_event(event_adapter.validate_python(_load_sample_event("exit")))
    db_session.commit()

    assert entry.id != exit_.id
    assert db_session.scalar(select(func.count()).select_from(Event)) == 2


def test_pos_transaction_idempotency_is_store_scoped(db_session: Session) -> None:
    """The same fix applied to Event's idempotency key: PosTransaction's is
    (store_id, order_id), enforced at the database level (see
    app/models/pos.py), not just via the pre-check in PosIngestionService."""
    rows_store_a = [
        {
            "order_id": "SHARED-ORDER-1",
            "order_date": "10-04-2026",
            "order_time": "12:00:00",
            "store_id": "ST9001",
            "product_id": "P1",
            "brand_name": "Brand A",
            "total_amount": "100.00",
        }
    ]
    rows_store_b = [{**rows_store_a[0], "store_id": "ST9002"}]

    service = PosIngestionService(db_session)
    result_a = service.import_rows(rows_store_a)
    result_b = service.import_rows(rows_store_b)
    db_session.commit()

    assert result_a.imported_transactions == 1
    assert result_b.imported_transactions == 1
    assert db_session.scalar(
        select(func.count()).select_from(PosTransaction).where(PosTransaction.order_id == "SHARED-ORDER-1")
    ) == 2
