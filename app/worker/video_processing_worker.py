from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import structlog
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.event import Event
from app.models.video_processing import VideoProcessingJob
from app.schemas.event import EventPayload
from app.services.event_ingestion_service import EventIngestionService
from app.services.video_processing_service import EventOutcome, VideoProcessingService
from app.services.visitor_inference_service import VisitorInferenceService
from pipeline.video.config import VideoProcessingConfig
from pipeline.video.processing import process_camera
from pipeline.video.tracking import UltralyticsByteTracker

logger = structlog.get_logger(__name__)

ProcessCameraFn = Callable[[VideoProcessingConfig, UltralyticsByteTracker], Iterator[dict[str, Any]]]
SleepFn = Callable[[float], None]


class VideoProcessingWorker:
    """Thin orchestration loop around ``VideoProcessingService`` and the
    existing CV pipeline (``pipeline.video.processing.process_camera``).

    Owns no business logic of its own: job lifecycle (create/claim/reclaim/
    counters/terminal status) stays in ``VideoProcessingService``, and
    event persistence (dedup, ``TrackedEntity``/``VisitSession``/
    ``IdentityAlias``, ``RawEvent`` audit) stays in
    ``EventIngestionService.process_event`` -- this class's only job is to
    call them in the right order and turn a claimed job into calls to both.

    One instance is meant to live for the lifetime of one worker process:
    the tracker (an ``UltralyticsByteTracker``, which loads a YOLO model) is
    constructed once by the caller and passed in, never recreated per job,
    and ``self.db`` is one long-lived ``Session`` reused across every poll
    iteration and every job -- each mutating call already commits its own
    short transaction (``VideoProcessingService``'s methods,
    ``EventIngestionService.process_event``), so there is never a reason to
    tear down and rebuild the session between jobs.
    """

    def __init__(
        self,
        db: Session,
        tracker: UltralyticsByteTracker,
        *,
        poll_interval_seconds: float | None = None,
        stale_after_minutes: int | None = None,
        process_camera_fn: ProcessCameraFn = process_camera,
        sleep_fn: SleepFn = time.sleep,
    ):
        self.db = db
        self.tracker = tracker
        self.service = VideoProcessingService(db, stale_after_minutes=stale_after_minutes)
        self.poll_interval_seconds = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else get_settings().video_processing_poll_interval_seconds
        )
        # Injectable seams for tests: a fake process_camera_fn lets worker
        # orchestration be tested without a real video file or YOLO model; a
        # fake sleep_fn lets the "no pending job -> sleep" path be tested
        # without an actual wall-clock wait.
        self._process_camera_fn = process_camera_fn
        self._sleep_fn = sleep_fn
        self._event_adapter = TypeAdapter(EventPayload)
        self._stop_requested = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Request that ``run_forever`` exit after its current iteration --
        a simple flag, not signal-handling infrastructure (a caller, e.g.
        the CLI entry point, wires a signal handler to this)."""
        self._stop_requested = True

    def run_forever(self) -> None:
        self._stop_requested = False
        while not self._stop_requested:
            self.run_once()

    # ------------------------------------------------------------------
    # One poll iteration
    # ------------------------------------------------------------------
    def run_once(self) -> bool:
        """Reclaim any stale RUNNING jobs, then try to claim and fully
        process one PENDING job. Returns ``True`` if a job was claimed and
        processed, ``False`` if there was nothing to do (in which case the
        worker sleeps for ``poll_interval_seconds`` before returning) --
        every iteration does both steps, in this order, so a job a previous
        worker abandoned becomes claimable again within the same cycle that
        notices it, without waiting for a separate sweep process.
        """
        reclaimed = self.service.reclaim_stale_jobs()
        if reclaimed:
            logger.info("video_worker_reclaimed_stale_jobs", count=len(reclaimed))

        job = self.service.claim_next_job()
        if job is None:
            self._sleep_fn(self.poll_interval_seconds)
            return False

        logger.info("video_worker_claimed_job", job_id=job.id, store_id=job.store_id, camera_id=job.camera_id)
        self._process_job(job)
        return True

    # ------------------------------------------------------------------
    # One claimed job
    # ------------------------------------------------------------------
    def _process_job(self, job: VideoProcessingJob) -> None:
        """Build the job's config, run it through process_camera to full
        generator exhaustion (never stopping early -- finalize()'s trailing
        queue events only arrive at the very end), ingesting each event as
        it's yielded, then mark the job's terminal status from its final
        counters.

        Everything from config-building through generator exhaustion is one
        failure zone: a config that can't be built (bad camera state, an
        unsafe video_path) is just as fatal to this job as a tracker/file
        error mid-video, and both are handled identically -- mark_failed,
        move on. A per-event failure inside the loop is handled separately
        (see _ingest_one_event) and never reaches here.

        Finalization (visitor inference plus the terminal-status decision)
        is a second, separate failure zone below -- see
        _fail_after_finalization -- so that a failure there is isolated the
        same way, without conflating it with a CV-level crash.
        """
        try:
            config = self.service.get_config(job)
            # get_config only reads (Camera/CameraCoverage/Zone lookups),
            # but SQLAlchemy's session autobegin still leaves an implicit
            # transaction open after those SELECTs. Ending it here -- before
            # any CV inference runs -- keeps "no open transaction while CV
            # inference is running" true for the first stretch of frames
            # too, not just between events later in the loop (where each
            # _ingest_one_event call already ends its own transaction via
            # VideoProcessingService.record_event_outcome's commit).
            self.db.commit()

            touched_store = False
            for raw_event in self._process_camera_fn(config, self.tracker):
                if self._ingest_one_event(job, raw_event):
                    touched_store = True
        except Exception as exc:  # noqa: BLE001 - isolate this job, keep the worker alive for the next one
            logger.exception("video_worker_job_crashed", job_id=job.id, error=str(exc))
            self.service.mark_failed(
                job,
                error_message=f"Video processing failed: {exc}",
                error_details={"error_type": type(exc).__name__},
            )
            return

        # A second, separate failure zone: CV/event processing already
        # completed successfully by this point, so a failure here (a bug in
        # inference, or another worker's reclaim_stale_jobs racing this job
        # back to PENDING/RUNNING before this call) must not be treated like
        # a CV-level crash -- and, per the isolate-this-job contract the
        # block above already established, must not escape and take down
        # run_forever() either.
        try:
            if touched_store:
                VisitorInferenceService(self.db).infer_store(job.store_id)
                self.db.commit()

            self._mark_terminal_status(job)
        except Exception as exc:  # noqa: BLE001 - isolate this job, keep the worker alive for the next one
            self._fail_after_finalization(job, exc)

    def _fail_after_finalization(self, job: VideoProcessingJob, exc: Exception) -> None:
        """Best-effort recovery when finalization (visitor inference or the
        terminal-status decision) fails after CV/event processing already
        completed. Rolls back whatever transaction ``exc`` may have left
        aborted, then tries to mark the job FAILED with ``exc`` as the
        recorded cause.

        That ``mark_failed`` call can itself raise ``VideoProcessingError``
        if the job is no longer RUNNING -- e.g. another worker's
        ``reclaim_stale_jobs`` already reset it out from under this one --
        in which case there is no valid terminal-state transition left for
        this worker to make. That secondary failure is logged, not raised:
        this method must never let a finalization failure propagate out of
        ``_process_job``.
        """
        logger.exception("video_worker_job_finalization_failed", job_id=job.id, error=str(exc))
        self.db.rollback()
        try:
            self.service.mark_failed(
                job,
                error_message=f"Video processing finalization failed: {exc}",
                error_details={"error_type": type(exc).__name__},
            )
        except Exception as mark_exc:  # noqa: BLE001 - job's terminal state is no longer ours to set; nothing more to do
            logger.warning(
                "video_worker_job_finalization_failed_terminal_state_unavailable",
                job_id=job.id,
                error=str(mark_exc),
            )

    def _mark_terminal_status(self, job: VideoProcessingJob) -> None:
        """The generator reached full exhaustion without a job-level crash
        -- decide COMPLETED/PARTIAL/FAILED purely from the counters
        record_event_outcome has been accumulating, matching
        VideoProcessingService's own defined semantics (see
        mark_completed/mark_partial/mark_failed's docstrings) rather than
        inventing a fourth rule here: no failures -> COMPLETED; some
        failures but something was salvaged (accepted or duplicate) ->
        PARTIAL; failures with nothing salvaged -> FAILED, since
        mark_partial itself refuses that case."""
        if job.failed_events == 0:
            self.service.mark_completed(job)
        elif job.accepted_events or job.duplicate_events:
            self.service.mark_partial(job)
        else:
            self.service.mark_failed(
                job,
                error_message=(
                    f"All {job.failed_events} generated event(s) failed ingestion; nothing was salvaged."
                ),
            )

    # ------------------------------------------------------------------
    # One event
    # ------------------------------------------------------------------
    def _ingest_one_event(self, job: VideoProcessingJob, raw_event: dict[str, Any]) -> bool:
        """Validate, dedup-check, and ingest one event dict process_camera
        yielded; always records an outcome on the job and never raises --
        one malformed/failing event must not stop the rest of the video.
        Returns ``True`` only for a genuinely newly-accepted event (used by
        the caller to decide whether a visitor-inference pass is needed).

        The existence pre-check happens *before* calling process_event,
        using the validated payload's own (store_id, event_id) -- not by
        comparing the resulting Event's video_processing_job_id to job.id
        after the fact, the way ReplayService does for ReplayJob. That
        comparison isn't safe here: unlike a ReplayJob, a VideoProcessingJob
        can be reclaimed and reprocessed under the *same* job.id (see
        VideoProcessingService.reclaim_stale_jobs), so an event accepted in
        an earlier, crashed attempt already carries this job's own id --
        comparing against it on retry would misreport a real duplicate as
        newly accepted. process_event is still called either way (never
        skipped for a pre-known duplicate, unlike app.api.events.ingest_events'
        batch fast path), so the RawEvent audit row is written every time --
        preserving provenance, not just outcome, for every generated event.
        """
        try:
            payload = self._event_adapter.validate_python(raw_event)
        except ValidationError as exc:
            logger.warning(
                "video_worker_event_invalid",
                job_id=job.id,
                event_id=raw_event.get("event_id"),
                error=str(exc),
            )
            self.service.record_event_outcome(job, "failed")
            return False

        store_id = getattr(payload, "store_id", None) or getattr(payload, "store_code", None)
        source_event_id = getattr(payload, "event_id", None)
        pre_existing = _event_already_exists(self.db, store_id, source_event_id)

        try:
            ingestion_service = EventIngestionService(self.db)
            ingestion_service.process_event(payload, run_inference=False, video_processing_job_id=job.id)
            self.db.commit()
        except Exception as exc:  # noqa: BLE001 - isolate this event, keep processing the rest of the video
            self.db.rollback()
            logger.warning(
                "video_worker_event_ingestion_failed",
                job_id=job.id,
                event_id=source_event_id,
                error=str(exc),
            )
            self.service.record_event_outcome(job, "failed")
            return False

        outcome: EventOutcome = "duplicate" if pre_existing else "accepted"
        self.service.record_event_outcome(job, outcome)
        return outcome == "accepted"


def _event_already_exists(db: Session, store_id: str | None, source_event_id: str | None) -> bool:
    """Mirrors app.api.events._event_exists's exact predicate (same two
    columns, same meaning of "already ingested") -- not a new definition of
    duplicate, just this module's own copy of it, since importing a
    leading-underscore helper from an API route module into worker code
    would run the dependency the wrong direction."""
    if not store_id or not source_event_id:
        return False
    return (
        db.scalar(select(Event.id).where(Event.store_id == store_id, Event.source_event_id == source_event_id))
        is not None
    )
