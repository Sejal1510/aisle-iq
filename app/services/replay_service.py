from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.storage import DATA_DIR
from app.db.base import utcnow
from app.models.raw_event import RawEvent
from app.models.replay import ReplayJob, ReplaySourceType, ReplayStatus
from app.models.store import Store
from app.schemas.event import EventPayload
from app.services.event_ingestion_service import (
    EventIngestionService,
    event_payload_store_and_timestamp,
)
from app.services.visitor_inference_service import VisitorInferenceService

logger = structlog.get_logger(__name__)

# Errors per job kept inline on the row (error_details_json) are capped so a
# replay with thousands of malformed rows doesn't grow the job row unbounded
# -- failed_events still counts every failure, this only bounds the detail list.
_MAX_ERROR_DETAILS = 50


class ReplayError(ValueError):
    """A request-level replay problem (unknown store, bad range, invalid
    source selection). Callers map this to 400, the same ValueError->400
    convention already used by the other P3+ services/routes."""


@dataclass(frozen=True)
class ReplayItem:
    """One candidate raw payload to replay, already resolved to a parsed
    ``EventPayload`` (or an ``error`` explaining why it couldn't be)."""

    label: str
    payload: EventPayload | None
    error: str | None
    timestamp: datetime | None = None


class ReplayService:
    """Replays previously-recorded raw events back through the same
    ``EventIngestionService.process_event`` live ingestion uses -- there is no
    parallel replay-specific ingestion path.

    Runs synchronously within the call that creates the job (this project has
    no background task runner). The job row it creates/updates is what makes
    that acceptable: progress, counts, and errors are all durably visible via
    ``get_job``/``list_jobs`` afterward (and, since progress is committed
    per-item, even mid-run for a sufficiently large replay observed from
    another connection).
    """

    def __init__(self, db: Session, *, dataset_dir: Path | None = None):
        self.db = db
        # Overridable for tests; production callers always get the real
        # data/ directory maps/camera-reference uploads already use.
        self.dataset_dir = dataset_dir or DATA_DIR
        self._event_adapter = TypeAdapter(EventPayload)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def create_and_run(
        self,
        store_id: str,
        *,
        source_type: ReplaySourceType,
        source_ref: str | None = None,
        range_start: datetime | None = None,
        range_end: datetime | None = None,
        event_ids: list[str] | None = None,
        requested_by_user_id: str | None = None,
    ) -> ReplayJob:
        if self.db.get(Store, store_id) is None:
            raise ReplayError(f"Store '{store_id}' does not exist.")
        if range_start is not None and range_end is not None and range_end <= range_start:
            raise ReplayError("range_end must be after range_start.")
        if source_type == ReplaySourceType.JSONL_FILE:
            if not source_ref:
                raise ReplayError("source_ref (a dataset filename) is required for source_type 'jsonl_file'.")
            self._validate_dataset_filename(source_ref)
        elif source_ref:
            raise ReplayError("source_ref is only used with source_type 'jsonl_file'.")
        if event_ids and source_type != ReplaySourceType.RAW_EVENT_ARCHIVE:
            raise ReplayError("event_ids is only supported with source_type 'raw_event_archive'.")

        job = ReplayJob(
            store_id=store_id,
            source_type=source_type,
            source_ref=source_ref,
            range_start=range_start,
            range_end=range_end,
            event_ids_json=json.dumps(event_ids) if event_ids else None,
            status=ReplayStatus.RUNNING,
            requested_by_user_id=requested_by_user_id,
        )
        self.db.add(job)
        self.db.commit()
        logger.info("replay_job_started", job_id=job.id, store_id=store_id, source_type=source_type.value)

        try:
            if source_type == ReplaySourceType.RAW_EVENT_ARCHIVE:
                items = self._load_from_archive(store_id, range_start, range_end, event_ids)
            else:
                items = self._load_from_jsonl(store_id, source_ref, range_start, range_end)
        except ReplayError as exc:
            job.status = ReplayStatus.FAILED
            job.error_message = str(exc)
            job.completed_at = utcnow()
            self.db.commit()
            logger.warning("replay_job_failed_to_load", job_id=job.id, error=str(exc))
            return job

        self._run(job, items)
        return job

    def get_job(self, store_id: str, job_id: str) -> ReplayJob | None:
        job = self.db.get(ReplayJob, job_id)
        if job is None or job.store_id != store_id:
            return None
        return job

    def list_jobs(self, store_id: str, *, limit: int = 20) -> list[ReplayJob]:
        return list(
            self.db.execute(
                select(ReplayJob)
                .where(ReplayJob.store_id == store_id)
                .order_by(ReplayJob.created_at.desc())
                .limit(limit)
            ).scalars()
        )

    # ------------------------------------------------------------------
    # Loading candidates (source-specific), always returned in the
    # chronological order they should be replayed in.
    # ------------------------------------------------------------------
    def _load_from_archive(
        self,
        store_id: str,
        range_start: datetime | None,
        range_end: datetime | None,
        event_ids: list[str] | None,
    ) -> list[ReplayItem]:
        """Scoped directly by ``RawEvent.store_id`` (denormalized at write
        time -- see RawEvent's docstring), not a join through ``event_id``:
        a row must stay replayable even if its canonical Event was deleted.
        That means the timestamp used to order/range-filter has to come from
        re-parsing the payload (there is no Event row to read it from in the
        general case) -- the same approach the jsonl_file source already
        needs, which keeps the two sources symmetric."""
        query = select(RawEvent).where(RawEvent.store_id == store_id)
        if event_ids:
            query = query.where(RawEvent.id.in_(event_ids))
        query = query.order_by(RawEvent.received_at.asc())

        valid: list[ReplayItem] = []
        errored: list[ReplayItem] = []
        for raw_event in self.db.execute(query).scalars().all():
            label = f"raw_event:{raw_event.id}"
            try:
                payload = self._event_adapter.validate_python(json.loads(raw_event.payload_json))
                _, timestamp = event_payload_store_and_timestamp(payload)
            except (ValidationError, ValueError) as exc:
                errored.append(ReplayItem(label=label, payload=None, error=str(exc)))
                continue
            if range_start is not None and timestamp < range_start:
                continue
            if range_end is not None and timestamp > range_end:
                continue
            valid.append(ReplayItem(label=label, payload=payload, error=None, timestamp=timestamp))

        valid.sort(key=lambda item: item.timestamp)
        return valid + errored

    def _load_from_jsonl(
        self,
        store_id: str,
        source_ref: str,
        range_start: datetime | None,
        range_end: datetime | None,
    ) -> list[ReplayItem]:
        base = self.dataset_dir.resolve()
        path = (self.dataset_dir / source_ref).resolve()
        if path != base and base not in path.parents:
            raise ReplayError(f"Invalid dataset filename: {source_ref!r}.")
        if not path.is_file():
            raise ReplayError(f"Dataset file '{source_ref}' was not found.")

        valid: list[ReplayItem] = []
        errored: list[ReplayItem] = []
        with path.open("r", encoding="utf-8") as fp:
            for line_no, raw_line in enumerate(fp, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                label = f"{source_ref}:{line_no}"
                try:
                    payload = self._event_adapter.validate_python(json.loads(raw_line))
                    payload_store_id, timestamp = event_payload_store_and_timestamp(payload)
                except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                    errored.append(ReplayItem(label=label, payload=None, error=str(exc)))
                    continue
                if payload_store_id != store_id:
                    errored.append(
                        ReplayItem(
                            label=label,
                            payload=None,
                            error=f"Event belongs to store '{payload_store_id}', not '{store_id}'.",
                        )
                    )
                    continue
                # A range filters the candidate set, the same way the archive
                # source's SQL WHERE does -- an out-of-range line is simply
                # not part of this replay, not a failure to report.
                if range_start is not None and timestamp < range_start:
                    continue
                if range_end is not None and timestamp > range_end:
                    continue
                valid.append(ReplayItem(label=label, payload=payload, error=None, timestamp=timestamp))

        valid.sort(key=lambda item: item.timestamp)
        return valid + errored

    def _validate_dataset_filename(self, source_ref: str) -> None:
        if (
            "/" in source_ref
            or "\\" in source_ref
            or source_ref in (".", "..")
            or not source_ref.endswith(".jsonl")
        ):
            raise ReplayError(
                f"Invalid dataset filename: {source_ref!r}. Expected a plain '*.jsonl' filename with no path."
            )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _run(self, job: ReplayJob, items: list[ReplayItem]) -> None:
        ingestion_service = EventIngestionService(self.db)
        job.total_events = len(items)
        self.db.commit()

        error_details: list[dict] = []
        touched = False

        try:
            for item in items:
                if item.payload is None:
                    job.processed_events += 1
                    job.failed_events += 1
                    if len(error_details) < _MAX_ERROR_DETAILS:
                        error_details.append({"item": item.label, "error": item.error})
                    self.db.commit()
                    continue

                try:
                    # Idempotency comes from process_event's existing
                    # (store_id, source_event_id) lookup -- replaying an
                    # already-processed event returns the same Event row
                    # instead of creating a duplicate. Comparing the
                    # returned event's replay_job_id to this job's id (set
                    # only on genuine creation, never on the duplicate path)
                    # is how we tell "newly created by this job" apart from
                    # "already existed" without re-deriving the idempotency
                    # key ourselves.
                    event = ingestion_service.process_event(
                        item.payload, run_inference=False, replay_job_id=job.id
                    )
                    job.processed_events += 1
                    if event.replay_job_id == job.id:
                        job.accepted_events += 1
                        touched = True
                    else:
                        job.duplicate_events += 1
                    self.db.commit()
                except Exception as exc:  # noqa: BLE001 - isolate one bad item, keep replaying the rest
                    self.db.rollback()
                    job.processed_events += 1
                    job.failed_events += 1
                    logger.warning("replay_item_failed", job_id=job.id, item=item.label, error=str(exc))
                    if len(error_details) < _MAX_ERROR_DETAILS:
                        error_details.append({"item": item.label, "error": str(exc)})
                    self.db.commit()

            if touched:
                VisitorInferenceService(self.db).infer_store(job.store_id)
                self.db.commit()
        except Exception as exc:  # noqa: BLE001 - never leave a job stuck at RUNNING
            self.db.rollback()
            job.status = ReplayStatus.FAILED
            job.error_message = f"Replay crashed: {exc}"
            job.completed_at = utcnow()
            self.db.commit()
            logger.exception("replay_job_crashed", job_id=job.id, error=str(exc))
            return

        job.error_details_json = json.dumps(error_details) if error_details else None
        if job.failed_events and (job.accepted_events or job.duplicate_events):
            job.status = ReplayStatus.PARTIAL
        elif job.failed_events:
            job.status = ReplayStatus.FAILED
            job.error_message = job.error_message or "All replay items failed; see error_details."
        else:
            job.status = ReplayStatus.COMPLETED
        job.completed_at = utcnow()
        self.db.commit()
        logger.info(
            "replay_job_finished",
            job_id=job.id,
            status=job.status.value,
            accepted=job.accepted_events,
            duplicates=job.duplicate_events,
            failed=job.failed_events,
        )
