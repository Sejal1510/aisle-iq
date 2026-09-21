from __future__ import annotations

import json
from datetime import timedelta
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.storage import resolve_video_path
from app.db.base import utcnow
from app.models.store import Camera
from app.models.video_processing import VideoProcessingJob, VideoProcessingStatus
from pipeline.video.config import (
    CameraRole,
    VideoProcessingConfig,
    build_config_for_job,
)

EventOutcome = Literal["accepted", "duplicate", "failed"]


class VideoProcessingError(ValueError):
    """A caller-facing VideoProcessingJob lifecycle error (bad camera/store
    relationship, unsafe video path, invalid state transition). A
    ValueError subclass so it composes with the existing ValueError->400
    convention (see StoreConfigError, ReplayError) once an API route exists
    for this -- there is none yet."""


class VideoProcessingService:
    """Owns the lifecycle of a VideoProcessingJob row: creation, atomic
    claiming, stale-job recovery, progress counters, and terminal status.

    Deliberately does not run any CV/tracking code itself and never calls
    pipeline.video.processing.process_camera -- that orchestration belongs
    to the future worker process, which will call this service's methods
    around its own process_camera loop (claim -> build config ->
    process_camera -> ingest each event -> record its outcome -> mark
    terminal). Keeping that loop out of this service is what keeps every
    method here a single short, committed transaction: there is never a
    reason for this service to hold a transaction open across CV processing
    or ingestion.
    """

    def __init__(self, db: Session, *, stale_after_minutes: int | None = None):
        self.db = db
        # Overridable for tests; production callers get the configured
        # setting (see Settings.video_processing_stale_job_minutes).
        self.stale_after_minutes = (
            stale_after_minutes
            if stale_after_minutes is not None
            else get_settings().video_processing_stale_job_minutes
        )

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def create_job(
        self, store_id: str, camera_id: str, *, requested_by_user_id: str | None = None
    ) -> VideoProcessingJob:
        """Create a PENDING VideoProcessingJob, snapshotting the camera's
        *current* ``video_path`` -- see ``build_config_for_job``'s docstring
        for why a job's own ``video_path``, once set, must never track a
        later re-upload to the same camera.

        Raises ``VideoProcessingError`` for anything that would make the job
        unprocessable from the start: camera not found for this store, no
        ``video_path``, no ``start_time``, or an unrecognized role -- the
        same requirements ``build_config_for_job`` itself enforces before
        processing, surfaced here at creation time instead of only when a
        worker later tries to build the config. Also propagates whatever
        ``resolve_video_path`` raises (``UnsafeIdentifierError``, a
        ``StorageError``/``ValueError`` subclass) if the camera's
        ``video_path`` cannot be proven to stay inside the video storage
        root -- the same containment check the onboarding write boundary
        already applies, re-verified here as defense-in-depth, mirroring
        ``build_config_for_job``'s own re-check.

        A camera can have at most one PENDING/RUNNING job at a time,
        enforced by the partial unique index on
        ``video_processing_job.camera_id`` (see the
        ``d4f9e2a8c1b3`` migration) -- not a second, application-level
        check-then-insert lock around that same invariant. This attempts the
        insert directly and, if it loses the race (``IntegrityError``),
        returns the existing active job instead of surfacing a low-level DB
        error: a duplicate "start processing" request for an
        already-queued/running camera is a safe no-op, not a failure. If no
        active job can be found after all (the index was violated for some
        other reason), the original ``IntegrityError`` is re-raised
        unchanged rather than masked.
        """
        camera = self.db.get(Camera, camera_id)
        if camera is None or camera.store_id != store_id:
            raise VideoProcessingError(f"Camera {camera_id!r} not found for store {store_id!r}.")
        if not camera.video_path:
            raise VideoProcessingError(f"Camera {camera_id!r} has no video_path configured.")
        if camera.start_time is None:
            raise VideoProcessingError(f"Camera {camera_id!r} has no configured start_time.")
        try:
            CameraRole(camera.role)
        except ValueError:
            raise VideoProcessingError(
                f"Camera {camera_id!r} has an unrecognized role {camera.role!r}."
            ) from None

        # Defense-in-depth: see resolve_video_path's own docstring for why a
        # value read back from the database cannot be trusted just because
        # it already passed this same check at the onboarding write boundary.
        resolve_video_path(camera.video_path)

        job = VideoProcessingJob(
            store_id=store_id,
            camera_id=camera_id,
            video_path=camera.video_path,
            status=VideoProcessingStatus.PENDING,
            requested_by_user_id=requested_by_user_id,
            total_events=0,
            processed_events=0,
            accepted_events=0,
            duplicate_events=0,
            failed_events=0,
        )
        self.db.add(job)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self._active_job_for_camera(camera_id)
            if existing is not None:
                return existing
            raise
        return job

    def _active_job_for_camera(self, camera_id: str) -> VideoProcessingJob | None:
        return self.db.execute(
            select(VideoProcessingJob).where(
                VideoProcessingJob.camera_id == camera_id,
                VideoProcessingJob.status.in_(
                    [VideoProcessingStatus.PENDING, VideoProcessingStatus.RUNNING]
                ),
            )
        ).scalars().first()

    # ------------------------------------------------------------------
    # Claim / stale recovery
    # ------------------------------------------------------------------
    def claim_next_job(self) -> VideoProcessingJob | None:
        """Atomically claim the oldest PENDING job, transitioning it to
        RUNNING. Returns ``None`` if there is no claimable job.

        A single ``UPDATE ... WHERE id = (SELECT ... LIMIT 1) ...
        RETURNING`` statement, not SELECT-then-UPDATE: the latter has a race
        window between reading a candidate row and writing to it where two
        concurrent workers could both read the same PENDING job before
        either claims it. This statement instead lets the database's own
        locking serialize concurrent claims -- PostgreSQL's row lock, or
        SQLite's whole-file write lock (see ``app.db.worker_session``'s
        ``busy_timeout``, which is what makes the loser wait briefly here
        instead of raising "database is locked") -- so that only one
        concurrent UPDATE can actually change a given row's status away from
        PENDING: a second worker's otherwise-identical statement re-checks
        ``WHERE status = PENDING`` against the now-committed row and simply
        matches zero rows instead of double-claiming it.

        ``started_at`` is set only on the row's first-ever PENDING ->
        RUNNING transition, via ``COALESCE`` rather than a plain assignment,
        so a later reclaim (RUNNING -> PENDING -> RUNNING again after a
        stale timeout) does not overwrite it with the retry's start time --
        it always reflects when this job was first attempted, however many
        attempts it took. ``claimed_at`` is unconditionally set to now on
        every claim, including a reclaimed retry.
        """
        now = utcnow()
        claim_id = (
            select(VideoProcessingJob.id)
            .where(VideoProcessingJob.status == VideoProcessingStatus.PENDING)
            .order_by(VideoProcessingJob.created_at, VideoProcessingJob.id)
            .limit(1)
            .scalar_subquery()
        )
        stmt = (
            update(VideoProcessingJob)
            .where(VideoProcessingJob.id == claim_id)
            .where(VideoProcessingJob.status == VideoProcessingStatus.PENDING)
            .values(
                status=VideoProcessingStatus.RUNNING,
                claimed_at=now,
                started_at=func.coalesce(VideoProcessingJob.started_at, now),
            )
            .returning(VideoProcessingJob)
        )
        job = self.db.execute(stmt).scalars().first()
        self.db.commit()
        return job

    def reclaim_stale_jobs(self) -> list[VideoProcessingJob]:
        """Return every stale RUNNING job -- claimed more than
        ``stale_after_minutes`` ago, presumed to belong to a worker that
        crashed or was killed mid-run without ever reaching a terminal
        status -- back to PENDING, so a future ``claim_next_job()`` call
        (by this worker or another) can pick it up again. The future worker
        calls this once at the start of each polling cycle; no
        scheduler/watchdog process is introduced here.

        One bulk ``UPDATE ... RETURNING``, not a SELECT-then-loop-UPDATE:
        the same single-statement-is-atomic reasoning as ``claim_next_job``
        applies, and it also means two workers calling this concurrently
        cannot both reclaim (and thus double-reset the counters of) the same
        stale job.

        Counters (``total_events`` through ``failed_events``) and transient
        error state (``error_message``, ``error_details_json``) are reset to
        their initial values: the retry re-runs ``process_camera`` over the
        video from the beginning -- there is no partial-resume -- so
        counters from the abandoned attempt would misrepresent the new
        attempt's actual progress. ``started_at`` is deliberately left
        untouched: it records when this job was first ever attempted, not
        when its most recent attempt began (see ``claim_next_job``).
        """
        threshold = utcnow() - timedelta(minutes=self.stale_after_minutes)
        stmt = (
            update(VideoProcessingJob)
            .where(VideoProcessingJob.status == VideoProcessingStatus.RUNNING)
            .where(VideoProcessingJob.claimed_at < threshold)
            .values(
                status=VideoProcessingStatus.PENDING,
                claimed_at=None,
                total_events=0,
                processed_events=0,
                accepted_events=0,
                duplicate_events=0,
                failed_events=0,
                error_message=None,
                error_details_json=None,
            )
            .returning(VideoProcessingJob)
        )
        reclaimed = list(self.db.execute(stmt).scalars().all())
        self.db.commit()
        return reclaimed

    # ------------------------------------------------------------------
    # Read (P9 API)
    # ------------------------------------------------------------------
    def get_job(self, store_id: str, job_id: str) -> VideoProcessingJob | None:
        """Look up one job, scoped to ``store_id`` -- a job id that exists
        but belongs to a different store is treated identically to one that
        doesn't exist at all (``None``), the same cross-store-safe lookup
        ``ReplayService.get_job`` already uses. Callers (the API route) turn
        ``None`` into a 404, never a 403 -- from a store-scoped caller's
        perspective, another store's job is indistinguishable from a
        nonexistent one."""
        job = self.db.get(VideoProcessingJob, job_id)
        if job is None or job.store_id != store_id:
            return None
        return job

    def list_jobs(self, store_id: str, *, limit: int = 20) -> list[VideoProcessingJob]:
        """The ``limit`` most recently created jobs for this store, newest
        first -- a fixed cap, not cursor/offset pagination, matching
        ``ReplayService.list_jobs``: this project has no list endpoint that
        paginates beyond a fixed recent-history window."""
        return list(
            self.db.execute(
                select(VideoProcessingJob)
                .where(VideoProcessingJob.store_id == store_id)
                .order_by(VideoProcessingJob.created_at.desc())
                .limit(limit)
            ).scalars()
        )

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------
    def record_event_outcome(self, job: VideoProcessingJob, outcome: EventOutcome) -> VideoProcessingJob:
        """Record one event ``process_camera`` yielded and the worker
        attempted to ingest, incrementing ``total_events``,
        ``processed_events``, and the counter for ``outcome`` together.

        In the worker's straight-line flow (yield one event -> attempt
        ingestion immediately, no buffering) these always advance in
        lockstep, so a single call covers "yielded" and "processed" at once
        rather than needing two separate counter operations per event.
        ``outcome`` is whichever of the three ``EventIngestionService``
        already distinguishes: ``"accepted"`` (a genuinely new ``Event``),
        ``"duplicate"`` (the existing pre-check -- see
        ``EventIngestionService.process_event``'s step 3 -- recognized it as
        already ingested), or ``"failed"`` (ingestion raised). Committed
        immediately (a short, per-event transaction), the same per-item
        commit pattern ``ReplayService`` already uses.
        """
        job.total_events += 1
        job.processed_events += 1
        if outcome == "accepted":
            job.accepted_events += 1
        elif outcome == "duplicate":
            job.duplicate_events += 1
        elif outcome == "failed":
            job.failed_events += 1
        else:
            raise VideoProcessingError(f"Unknown event outcome: {outcome!r}")
        self.db.commit()
        return job

    # ------------------------------------------------------------------
    # Terminal states
    # ------------------------------------------------------------------
    def mark_completed(self, job: VideoProcessingJob) -> VideoProcessingJob:
        """COMPLETED: ``process_camera``'s generator reached full
        exhaustion and no event failed ingestion. A zero-event video
        (nothing ever entered/exited frame) is a legitimate COMPLETED run,
        not a failure -- there is no minimum-event-count check here."""
        self._require_running(job, target="completed")
        if job.failed_events:
            raise VideoProcessingError(
                f"Cannot mark job {job.id!r} completed: {job.failed_events} event(s) failed ingestion "
                "(use mark_partial or mark_failed instead)."
            )
        job.status = VideoProcessingStatus.COMPLETED
        job.completed_at = utcnow()
        self.db.commit()
        return job

    def mark_partial(self, job: VideoProcessingJob) -> VideoProcessingJob:
        """PARTIAL: the generator still reached full exhaustion (this is
        *not* the crash/fatal-error case -- that's ``mark_failed``), but at
        least one yielded event failed ingestion while at least one other
        succeeded (accepted, or recognized as a duplicate). A run that is
        entirely failures with nothing salvaged is FAILED, not PARTIAL."""
        self._require_running(job, target="partial")
        if not job.failed_events:
            raise VideoProcessingError(f"Cannot mark job {job.id!r} partial: no events failed.")
        if not (job.accepted_events or job.duplicate_events):
            raise VideoProcessingError(
                f"Cannot mark job {job.id!r} partial: no events were accepted or recognized as "
                "duplicates (use mark_failed instead)."
            )
        job.status = VideoProcessingStatus.PARTIAL
        job.completed_at = utcnow()
        self.db.commit()
        return job

    def mark_failed(
        self, job: VideoProcessingJob, *, error_message: str, error_details: dict | list | None = None
    ) -> VideoProcessingJob:
        """FAILED: a tracker/file-level exception stopped processing before
        the generator was exhausted, or another fatal job-level error
        occurred -- unlike PARTIAL, this covers a run that never got the
        chance to salvage anything, regardless of what its counters
        happen to show at the moment it failed.

        ``error_details``, if given, should be a small, already-summarized
        payload (a few key fields) -- never a raw traceback; callers should
        not pass ``str(exc)`` plus ``traceback.format_exc()`` here.
        """
        self._require_running(job, target="failed")
        job.status = VideoProcessingStatus.FAILED
        job.error_message = error_message
        job.error_details_json = json.dumps(error_details) if error_details is not None else None
        job.completed_at = utcnow()
        self.db.commit()
        return job

    def _require_running(self, job: VideoProcessingJob, *, target: str) -> None:
        if job.status != VideoProcessingStatus.RUNNING:
            raise VideoProcessingError(
                f"Cannot mark job {job.id!r} {target} from status {job.status.value!r}; expected RUNNING."
            )

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    def get_config(self, job: VideoProcessingJob) -> VideoProcessingConfig:
        """The job-scoped ``VideoProcessingConfig`` for ``job``, always
        built from ``job.video_path`` -- never a silent fallback to the
        camera's current ``video_path``. A thin pass-through to
        ``build_config_for_job`` rather than a duplicate of its
        config-building logic; see that function's docstring for why the
        distinction matters."""
        return build_config_for_job(self.db, job)
