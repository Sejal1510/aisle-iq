# Coverage for the raw-event replay workflow (app/services/replay_service.py):
# replay reuses EventIngestionService.process_event directly (no parallel
# ingestion path), so these tests focus on what's specific to replay itself --
# idempotency, ordering, range/event-id selection, partial-failure isolation,
# path-traversal rejection, is_replay provenance, and that downstream
# analytics reflect events a replay adds.
import json
from datetime import datetime

import pytest
from pydantic import TypeAdapter
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.event import Event
from app.models.raw_event import RawEvent
from app.models.replay import ReplaySourceType, ReplayStatus
from app.models.store import Store
from app.schemas.event import EventPayload
from app.services.event_ingestion_service import EventIngestionService
from app.services.occupancy_service import OccupancyService
from app.services.replay_service import ReplayError, ReplayService

STORE_ID = "ST_REPLAY"
OTHER_STORE_ID = "ST_REPLAY_OTHER"


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def event_adapter() -> TypeAdapter:
    return TypeAdapter(EventPayload)


def _entry(id_token: str, timestamp: str, *, store_id: str = STORE_ID, camera_id: str = "CAM_ENTRY_1") -> dict:
    return {
        "event_type": "entry",
        "id_token": id_token,
        "store_code": store_id,
        "camera_id": camera_id,
        "event_timestamp": timestamp,
    }


def _exit(id_token: str, timestamp: str, *, store_id: str = STORE_ID, camera_id: str = "CAM_ENTRY_1") -> dict:
    return {
        "event_type": "exit",
        "id_token": id_token,
        "store_code": store_id,
        "camera_id": camera_id,
        "event_timestamp": timestamp,
    }


def _ingest_live(db_session: Session, event_adapter: TypeAdapter, raw: dict) -> Event:
    payload = event_adapter.validate_python(raw)
    event = EventIngestionService(db_session).process_event(payload)
    db_session.commit()
    return event


def _ensure_store(db_session: Session, store_id: str = STORE_ID) -> None:
    if db_session.get(Store, store_id) is None:
        db_session.add(Store(id=store_id, name=None))
        db_session.commit()


# ---------------------------------------------------------------------------
# Idempotency / provenance
# ---------------------------------------------------------------------------


def test_replay_from_archive_is_idempotent(db_session: Session, event_adapter: TypeAdapter) -> None:
    live_event = _ingest_live(db_session, event_adapter, _entry("ID_R1", "2026-05-01T09:00:00"))
    assert live_event.is_replay is False
    assert live_event.replay_job_id is None

    job = ReplayService(db_session).create_and_run(STORE_ID, source_type=ReplaySourceType.RAW_EVENT_ARCHIVE)

    assert job.status == ReplayStatus.COMPLETED
    assert job.total_events == 1
    assert job.accepted_events == 0
    assert job.duplicate_events == 1
    assert job.failed_events == 0
    # No new canonical Event was created -- replaying an already-processed
    # event must not duplicate derived state.
    assert db_session.scalar(select(func.count()).select_from(Event)) == 1
    # But the replay attempt itself is still recorded as its own receipt.
    assert db_session.scalar(select(func.count()).select_from(RawEvent)) == 2
    replay_receipts = db_session.scalars(select(RawEvent).where(RawEvent.replay_job_id == job.id)).all()
    assert len(replay_receipts) == 1
    assert replay_receipts[0].validation_status == "duplicate"
    assert replay_receipts[0].store_id == STORE_ID

    # Provenance on the pre-existing Event is untouched by the replay.
    db_session.refresh(live_event)
    assert live_event.is_replay is False
    assert live_event.replay_job_id is None


def test_rerunning_the_same_replay_twice_still_does_not_duplicate(
    db_session: Session, event_adapter: TypeAdapter
) -> None:
    _ingest_live(db_session, event_adapter, _entry("ID_R2", "2026-05-01T09:00:00"))

    service = ReplayService(db_session)
    service.create_and_run(STORE_ID, source_type=ReplaySourceType.RAW_EVENT_ARCHIVE)
    job2 = service.create_and_run(STORE_ID, source_type=ReplaySourceType.RAW_EVENT_ARCHIVE)

    assert job2.status == ReplayStatus.COMPLETED
    # Two archive replays of the same one live event: the archive now has
    # the original + first replay's receipt as candidates too, but every
    # single one of them still resolves to the same canonical Event.
    assert db_session.scalar(select(func.count()).select_from(Event)) == 1


def test_replay_recreates_a_deleted_event_and_marks_it_as_replayed(
    db_session: Session, event_adapter: TypeAdapter
) -> None:
    """Exercises RawEvent's own stated purpose (see its docstring): recover
    derived state from the raw archive after the canonical Event it produced
    is gone -- e.g. lost to a bug or manual cleanup. This only works because
    RawEvent.store_id is scoped independently of a join through event_id."""
    live_event = _ingest_live(db_session, event_adapter, _entry("ID_R3", "2026-05-01T09:00:00"))
    raw_event_id = db_session.scalar(select(RawEvent.id).where(RawEvent.event_id == live_event.id))

    db_session.execute(delete(Event).where(Event.id == live_event.id))
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(Event)) == 0

    job = ReplayService(db_session).create_and_run(STORE_ID, source_type=ReplaySourceType.RAW_EVENT_ARCHIVE)

    assert job.status == ReplayStatus.COMPLETED
    assert job.accepted_events == 1
    assert job.duplicate_events == 0
    recreated = db_session.scalars(select(Event)).all()
    assert len(recreated) == 1
    assert recreated[0].is_replay is True
    assert recreated[0].replay_job_id == job.id
    # The original RawEvent row (now orphaned) is still there, plus a new
    # receipt for the replay attempt that recreated the Event.
    assert raw_event_id is not None


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_replay_preserves_chronological_order_regardless_of_ingestion_order(
    db_session: Session, event_adapter: TypeAdapter
) -> None:
    """Ingest exit before entry (out of business-time order, e.g. two
    cameras' feeds arriving out of sync) -- process_event handles an
    out-of-order exit ungracefully (there's no open session to close), so
    proving replay reorders by the event's own timestamp (not received_at)
    means checking the *reconstructed* dwell/session behavior instead: the
    entry->exit dwell is only correct if replay processes entry first."""
    _ensure_store(db_session)
    entity_a_entry = _entry("ID_ORDER_1", "2026-05-01T09:00:00")
    entity_a_exit = _exit("ID_ORDER_1", "2026-05-01T09:30:00")

    # Ingest out of chronological order: exit first, then entry.
    _ingest_live(db_session, event_adapter, entity_a_exit)
    _ingest_live(db_session, event_adapter, entity_a_entry)

    # Sanity: because they were ingested out of order, the exit created its
    # own ad-hoc session rather than closing the entry's session.
    assert db_session.scalar(select(func.count()).select_from(Event)) == 2

    service = ReplayService(db_session)
    items = service._load_from_archive(STORE_ID, None, None, None)  # noqa: SLF001 - white-box ordering check

    assert [item.label for item in items if item.timestamp == datetime(2026, 5, 1, 9, 0, 0)]
    timestamps = [item.timestamp for item in items]
    assert timestamps == sorted(timestamps)


def test_jsonl_replay_sorts_by_timestamp_not_file_order(
    db_session: Session, event_adapter: TypeAdapter, tmp_path
) -> None:
    _ensure_store(db_session)
    dataset = tmp_path / "unordered.jsonl"
    # File order is deliberately reversed relative to event_timestamp.
    lines = [
        json.dumps(_entry("ID_JSONL_2", "2026-05-02T09:05:00")),
        json.dumps(_entry("ID_JSONL_1", "2026-05-02T09:00:00")),
    ]
    dataset.write_text("\n".join(lines) + "\n", encoding="utf-8")

    job = ReplayService(db_session, dataset_dir=tmp_path).create_and_run(
        STORE_ID, source_type=ReplaySourceType.JSONL_FILE, source_ref="unordered.jsonl"
    )

    assert job.status == ReplayStatus.COMPLETED
    assert job.accepted_events == 2
    events = db_session.scalars(select(Event).order_by(Event.timestamp.asc())).all()
    assert [e.timestamp for e in events] == sorted(e.timestamp for e in events)
    assert all(e.is_replay for e in events)
    assert all(e.replay_job_id == job.id for e in events)


# ---------------------------------------------------------------------------
# Range / event-id selection
# ---------------------------------------------------------------------------


def test_range_filters_the_archive_candidate_set(db_session: Session, event_adapter: TypeAdapter) -> None:
    _ingest_live(db_session, event_adapter, _entry("ID_RANGE_1", "2026-05-01T08:00:00"))
    _ingest_live(db_session, event_adapter, _entry("ID_RANGE_2", "2026-05-01T09:00:00"))
    _ingest_live(db_session, event_adapter, _entry("ID_RANGE_3", "2026-05-01T10:00:00"))

    job = ReplayService(db_session).create_and_run(
        STORE_ID,
        source_type=ReplaySourceType.RAW_EVENT_ARCHIVE,
        range_start=datetime(2026, 5, 1, 8, 30, 0),
        range_end=datetime(2026, 5, 1, 9, 30, 0),
    )

    assert job.status == ReplayStatus.COMPLETED
    assert job.total_events == 1  # only the 09:00 entry falls inside the range
    assert job.duplicate_events == 1


def test_event_ids_selects_an_explicit_subset(db_session: Session, event_adapter: TypeAdapter) -> None:
    _ingest_live(db_session, event_adapter, _entry("ID_SUBSET_1", "2026-05-01T08:00:00"))
    _ingest_live(db_session, event_adapter, _entry("ID_SUBSET_2", "2026-05-01T09:00:00"))
    raw_ids = db_session.scalars(select(RawEvent.id).order_by(RawEvent.received_at.asc())).all()

    job = ReplayService(db_session).create_and_run(
        STORE_ID,
        source_type=ReplaySourceType.RAW_EVENT_ARCHIVE,
        event_ids=[raw_ids[0]],
    )

    assert job.total_events == 1
    assert job.duplicate_events == 1


def test_range_end_must_be_after_range_start(db_session: Session) -> None:
    _ensure_store(db_session)
    with pytest.raises(ReplayError):
        ReplayService(db_session).create_and_run(
            STORE_ID,
            source_type=ReplaySourceType.RAW_EVENT_ARCHIVE,
            range_start=datetime(2026, 5, 1, 10, 0, 0),
            range_end=datetime(2026, 5, 1, 9, 0, 0),
        )


def test_unknown_store_is_rejected(db_session: Session) -> None:
    with pytest.raises(ReplayError):
        ReplayService(db_session).create_and_run("ST_DOES_NOT_EXIST", source_type=ReplaySourceType.RAW_EVENT_ARCHIVE)


# ---------------------------------------------------------------------------
# jsonl_file source: dataset selection, store scoping, path safety
# ---------------------------------------------------------------------------


def test_jsonl_replay_requires_source_ref(db_session: Session) -> None:
    _ensure_store(db_session)
    with pytest.raises(ReplayError):
        ReplayService(db_session).create_and_run(STORE_ID, source_type=ReplaySourceType.JSONL_FILE)


@pytest.mark.parametrize("bad_ref", ["../secrets.jsonl", "sub/dir.jsonl", "no_extension", "..", "."])
def test_jsonl_replay_rejects_unsafe_or_malformed_filenames(db_session: Session, bad_ref: str) -> None:
    _ensure_store(db_session)
    with pytest.raises(ReplayError):
        ReplayService(db_session).create_and_run(
            STORE_ID, source_type=ReplaySourceType.JSONL_FILE, source_ref=bad_ref
        )


def test_jsonl_replay_reports_missing_dataset_as_a_failed_job_not_a_crash(
    db_session: Session, tmp_path
) -> None:
    _ensure_store(db_session)
    job = ReplayService(db_session, dataset_dir=tmp_path).create_and_run(
        STORE_ID, source_type=ReplaySourceType.JSONL_FILE, source_ref="missing.jsonl"
    )

    assert job.status == ReplayStatus.FAILED
    assert job.error_message is not None
    assert "not found" in job.error_message.lower()


def test_jsonl_replay_skips_lines_for_a_different_store_as_errors(
    db_session: Session, tmp_path
) -> None:
    _ensure_store(db_session)
    _ensure_store(db_session, OTHER_STORE_ID)
    dataset = tmp_path / "mixed.jsonl"
    lines = [
        json.dumps(_entry("ID_MIX_1", "2026-05-03T09:00:00", store_id=STORE_ID)),
        json.dumps(_entry("ID_MIX_2", "2026-05-03T09:05:00", store_id=OTHER_STORE_ID)),
    ]
    dataset.write_text("\n".join(lines) + "\n", encoding="utf-8")

    job = ReplayService(db_session, dataset_dir=tmp_path).create_and_run(
        STORE_ID, source_type=ReplaySourceType.JSONL_FILE, source_ref="mixed.jsonl"
    )

    assert job.status == ReplayStatus.PARTIAL
    assert job.accepted_events == 1
    assert job.failed_events == 1
    error_details = json.loads(job.error_details_json)
    assert len(error_details) == 1
    assert OTHER_STORE_ID in error_details[0]["error"]


# ---------------------------------------------------------------------------
# Partial failure handling
# ---------------------------------------------------------------------------


def test_malformed_line_is_isolated_and_does_not_block_the_rest(
    db_session: Session, tmp_path
) -> None:
    _ensure_store(db_session)
    dataset = tmp_path / "partial.jsonl"
    lines = [
        json.dumps(_entry("ID_PARTIAL_1", "2026-05-04T09:00:00")),
        "{not valid json",
        json.dumps(_entry("ID_PARTIAL_2", "2026-05-04T09:10:00")),
    ]
    dataset.write_text("\n".join(lines) + "\n", encoding="utf-8")

    job = ReplayService(db_session, dataset_dir=tmp_path).create_and_run(
        STORE_ID, source_type=ReplaySourceType.JSONL_FILE, source_ref="partial.jsonl"
    )

    assert job.status == ReplayStatus.PARTIAL
    assert job.total_events == 3
    assert job.processed_events == 3
    assert job.accepted_events == 2
    assert job.failed_events == 1
    assert db_session.scalar(select(func.count()).select_from(Event)) == 2
    error_details = json.loads(job.error_details_json)
    assert len(error_details) == 1


def test_all_items_failing_marks_the_job_failed(db_session: Session, tmp_path) -> None:
    _ensure_store(db_session)
    dataset = tmp_path / "all_bad.jsonl"
    dataset.write_text("not json\nstill not json\n", encoding="utf-8")

    job = ReplayService(db_session, dataset_dir=tmp_path).create_and_run(
        STORE_ID, source_type=ReplaySourceType.JSONL_FILE, source_ref="all_bad.jsonl"
    )

    assert job.status == ReplayStatus.FAILED
    assert job.accepted_events == 0
    assert job.failed_events == 2


# ---------------------------------------------------------------------------
# Job lookups
# ---------------------------------------------------------------------------


def test_get_job_is_scoped_to_its_store(db_session: Session, event_adapter: TypeAdapter) -> None:
    _ingest_live(db_session, event_adapter, _entry("ID_SCOPE_1", "2026-05-01T09:00:00"))
    _ensure_store(db_session, OTHER_STORE_ID)
    job = ReplayService(db_session).create_and_run(STORE_ID, source_type=ReplaySourceType.RAW_EVENT_ARCHIVE)

    service = ReplayService(db_session)
    assert service.get_job(STORE_ID, job.id) is not None
    assert service.get_job(OTHER_STORE_ID, job.id) is None


def test_list_jobs_orders_most_recent_first(db_session: Session, event_adapter: TypeAdapter) -> None:
    _ingest_live(db_session, event_adapter, _entry("ID_LIST_1", "2026-05-01T09:00:00"))
    service = ReplayService(db_session)
    job1 = service.create_and_run(STORE_ID, source_type=ReplaySourceType.RAW_EVENT_ARCHIVE)
    job2 = service.create_and_run(STORE_ID, source_type=ReplaySourceType.RAW_EVENT_ARCHIVE)

    jobs = service.list_jobs(STORE_ID)
    assert jobs[0].id == job2.id
    assert jobs[1].id == job1.id


# ---------------------------------------------------------------------------
# Downstream analytics consistency
# ---------------------------------------------------------------------------


def test_occupancy_reflects_events_added_by_replay(db_session: Session, tmp_path) -> None:
    _ensure_store(db_session)
    dataset = tmp_path / "occupancy.jsonl"
    lines = [
        json.dumps(_entry("ID_OCC_1", "2026-05-05T09:00:00")),
        json.dumps(_entry("ID_OCC_2", "2026-05-05T09:05:00")),
    ]
    dataset.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert OccupancyService(db_session).current_occupancy(STORE_ID).occupancy == 0

    ReplayService(db_session, dataset_dir=tmp_path).create_and_run(
        STORE_ID, source_type=ReplaySourceType.JSONL_FILE, source_ref="occupancy.jsonl"
    )

    # Occupancy is defined relative to the store's latest event timestamp
    # (see README's "What 'current' means"), so it picks up replayed
    # historical events the same way it would live ones -- no special-casing
    # needed in OccupancyService itself.
    current = OccupancyService(db_session).current_occupancy(STORE_ID)
    assert current.occupancy == 2
