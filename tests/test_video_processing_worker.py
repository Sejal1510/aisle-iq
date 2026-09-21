# P9: focused tests for app.worker.video_processing_worker.VideoProcessingWorker
# -- the thin orchestration loop around VideoProcessingService and the
# existing CV pipeline. process_camera and the tracker are both injected via
# fakes (see FakeProcessCamera below): no real video file, YOLO model, or
# Ultralytics dependency is needed to exercise the worker's own logic.
from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.event import Event
from app.models.raw_event import RawEvent
from app.models.store import Camera, Store
from app.models.video_processing import VideoProcessingJob, VideoProcessingStatus
from app.schemas.event import EventPayload
from app.services.event_ingestion_service import EventIngestionService
from app.services.video_processing_service import VideoProcessingService
from app.services.visitor_inference_service import VisitorInferenceService
from app.worker.video_processing_worker import VideoProcessingWorker

BASE_TIME = datetime(2026, 7, 1, 9, 0, 0)


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_camera(
    db_session: Session,
    *,
    store_id: str,
    camera_id: str,
    video_path: str,
    role: str = "zone",
    start_time: datetime = BASE_TIME,
) -> Camera:
    if db_session.get(Store, store_id) is None:
        db_session.add(Store(id=store_id))
    camera = Camera(id=camera_id, store_id=store_id, role=role, video_path=video_path, start_time=start_time)
    db_session.add(camera)
    db_session.commit()
    return camera


def _event_dict(
    *,
    event_id: str,
    store_id: str = "ST_W",
    camera_id: str = "CAM_W",
    visitor_id: str = "CAM_W:1",
    event_type: str = "ENTRY",
    timestamp: str = "2026-07-01T09:00:05",
    zone_id: str | None = None,
    confidence: float = 0.9,
) -> dict[str, Any]:
    """Shaped exactly like pipeline.video.events.VideoEventGenerator's own
    output dicts -- a CanonicalEvent, not any other EventPayload variant."""
    return {
        "event_id": event_id,
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": confidence,
        "metadata": {},
    }


class FakeProcessCamera:
    """A queue of canned generators, one consumed per call -- stands in for
    pipeline.video.processing.process_camera so worker orchestration can be
    tested without a real video file or tracker. Records every (config,
    tracker) it was called with."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self._generator_factories: list[Callable[[], Iterator[dict[str, Any]]]] = []

    def queue(self, generator_factory: Callable[[], Iterator[dict[str, Any]]]) -> None:
        self._generator_factories.append(generator_factory)

    def queue_events(self, events: list[dict[str, Any]]) -> None:
        self.queue(lambda: iter(events))

    def __call__(self, config: Any, tracker: Any) -> Iterator[dict[str, Any]]:
        self.calls.append((config, tracker))
        generator_factory = self._generator_factories.pop(0)
        yield from generator_factory()


def _make_worker(
    db_session: Session,
    fake: FakeProcessCamera,
    *,
    tracker: Any = None,
    stale_after_minutes: int = 10,
) -> VideoProcessingWorker:
    sleep_calls: list[float] = []
    worker = VideoProcessingWorker(
        db_session,
        tracker if tracker is not None else object(),
        stale_after_minutes=stale_after_minutes,
        process_camera_fn=fake,
        sleep_fn=lambda seconds: sleep_calls.append(seconds),
    )
    worker.sleep_calls = sleep_calls  # type: ignore[attr-defined]
    return worker


def _seed_existing_event(db_session: Session, event_dict: dict[str, Any]) -> Event:
    """Ingest an event directly through EventIngestionService, bypassing the
    worker entirely -- simulates an event that already exists (e.g. from a
    prior job run, or the offline JSONL/CLI path) before the worker under
    test ever sees it."""
    payload = TypeAdapter(EventPayload).validate_python(event_dict)
    event = EventIngestionService(db_session).process_event(payload, run_inference=False)
    db_session.commit()
    return event


# ---------------------------------------------------------------------------
# A, K, L: poll loop
# ---------------------------------------------------------------------------


def test_worker_claims_a_pending_job(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_A", camera_id="CAM_A", video_path="data/videos/ST_A/a.mp4")
    job = VideoProcessingService(db_session).create_job("ST_A", "CAM_A")

    fake = FakeProcessCamera()
    fake.queue_events([])
    worker = _make_worker(db_session, fake)

    processed = worker.run_once()

    assert processed is True
    assert len(fake.calls) == 1
    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.status == VideoProcessingStatus.COMPLETED
    assert reloaded.claimed_at is not None


def test_stale_jobs_reclaimed_before_new_work_is_claimed(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_K", camera_id="CAM_K1", video_path="data/videos/ST_K/1.mp4")
    _make_camera(db_session, store_id="ST_K", camera_id="CAM_K2", video_path="data/videos/ST_K/2.mp4")
    service = VideoProcessingService(db_session, stale_after_minutes=10)

    older_job = service.create_job("ST_K", "CAM_K1")
    older_job.created_at = BASE_TIME
    claimed = service.claim_next_job()
    assert claimed.id == older_job.id
    claimed.claimed_at = BASE_TIME - timedelta(minutes=30)  # force staleness
    db_session.commit()

    newer_job = service.create_job("ST_K", "CAM_K2")
    newer_job.created_at = BASE_TIME + timedelta(seconds=5)
    db_session.commit()

    fake = FakeProcessCamera()
    fake.queue_events([])  # whichever job gets claimed this iteration
    worker = _make_worker(db_session, fake)

    worker.run_once()

    # The reclaimed job (older created_at) must be the one claimed and
    # processed this iteration -- proving reclaim ran before claim, not the
    # still-PENDING newer job.
    assert db_session.get(VideoProcessingJob, older_job.id).status == VideoProcessingStatus.COMPLETED
    assert db_session.get(VideoProcessingJob, newer_job.id).status == VideoProcessingStatus.PENDING


def test_no_pending_job_causes_sleep_not_busy_loop(db_session: Session) -> None:
    fake = FakeProcessCamera()
    worker = _make_worker(db_session, fake)

    processed = worker.run_once()

    assert processed is False
    assert fake.calls == []
    assert worker.sleep_calls == [worker.poll_interval_seconds]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# B: job.video_path, not live camera.video_path
# ---------------------------------------------------------------------------


def test_worker_uses_jobs_video_path_not_live_camera_video_path(db_session: Session) -> None:
    _make_camera(
        db_session, store_id="ST_B", camera_id="CAM_B", video_path="data/videos/ST_B/original.mp4"
    )
    job = VideoProcessingService(db_session).create_job("ST_B", "CAM_B")

    camera = db_session.get(Camera, "CAM_B")
    camera.video_path = "data/videos/ST_B/replacement.mp4"
    db_session.commit()

    fake = FakeProcessCamera()
    fake.queue_events([])
    worker = _make_worker(db_session, fake)

    worker.run_once()

    assert len(fake.calls) == 1
    config_used, _tracker = fake.calls[0]
    assert config_used.video_path == Path("data/videos/ST_B/original.mp4")
    assert db_session.get(VideoProcessingJob, job.id).video_path == "data/videos/ST_B/original.mp4"


# ---------------------------------------------------------------------------
# C: tracker instantiated once, reused across jobs
# ---------------------------------------------------------------------------


def test_tracker_is_reused_across_jobs_not_recreated(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_C", camera_id="CAM_C1", video_path="data/videos/ST_C/1.mp4")
    _make_camera(db_session, store_id="ST_C", camera_id="CAM_C2", video_path="data/videos/ST_C/2.mp4")
    service = VideoProcessingService(db_session)
    service.create_job("ST_C", "CAM_C1")
    service.create_job("ST_C", "CAM_C2")

    fake = FakeProcessCamera()
    fake.queue_events([])
    fake.queue_events([])
    tracker_sentinel = object()
    worker = _make_worker(db_session, fake, tracker=tracker_sentinel)

    worker.run_once()
    worker.run_once()

    assert len(fake.calls) == 2
    assert fake.calls[0][1] is tracker_sentinel
    assert fake.calls[1][1] is tracker_sentinel
    assert worker.tracker is tracker_sentinel


# ---------------------------------------------------------------------------
# D: full generator exhaustion
# ---------------------------------------------------------------------------


def test_worker_fully_exhausts_the_process_camera_generator(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_D", camera_id="CAM_D", video_path="data/videos/ST_D/a.mp4")
    VideoProcessingService(db_session).create_job("ST_D", "CAM_D")

    events = [_event_dict(event_id=f"E-D-{i}") for i in range(5)]
    fake = FakeProcessCamera()
    fake.queue_events(events)
    worker = _make_worker(db_session, fake)

    worker.run_once()

    job = db_session.scalars(select(VideoProcessingJob)).first()
    assert job.total_events == 5
    assert job.processed_events == 5


# ---------------------------------------------------------------------------
# E, F, G: per-event outcome counters
# ---------------------------------------------------------------------------


def test_accepted_event_increments_counters(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_E", camera_id="CAM_E", video_path="data/videos/ST_E/a.mp4")
    job = VideoProcessingService(db_session).create_job("ST_E", "CAM_E")

    fake = FakeProcessCamera()
    fake.queue_events([_event_dict(event_id="E-ACC-1", store_id="ST_E", camera_id="CAM_E")])
    worker = _make_worker(db_session, fake)

    worker.run_once()

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert (reloaded.total_events, reloaded.processed_events, reloaded.accepted_events) == (1, 1, 1)
    assert (reloaded.duplicate_events, reloaded.failed_events) == (0, 0)


def test_duplicate_event_increments_counters(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_F", camera_id="CAM_F", video_path="data/videos/ST_F/a.mp4")
    existing = _event_dict(event_id="E-DUP-1", store_id="ST_F", camera_id="CAM_F")
    _seed_existing_event(db_session, existing)
    job = VideoProcessingService(db_session).create_job("ST_F", "CAM_F")

    fake = FakeProcessCamera()
    fake.queue_events([existing])  # same event_id -> already exists
    worker = _make_worker(db_session, fake)

    worker.run_once()

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert (reloaded.total_events, reloaded.processed_events, reloaded.duplicate_events) == (1, 1, 1)
    assert (reloaded.accepted_events, reloaded.failed_events) == (0, 0)
    # No second Event row was created for the duplicate.
    rows = db_session.scalars(select(Event).where(Event.source_event_id == "E-DUP-1")).all()
    assert len(rows) == 1


def test_failed_event_increments_counters_and_processing_continues(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_G", camera_id="CAM_G", video_path="data/videos/ST_G/a.mp4")
    job = VideoProcessingService(db_session).create_job("ST_G", "CAM_G")

    invalid_event = _event_dict(event_id="E-BAD-1", store_id="ST_G", camera_id="CAM_G", confidence=5.0)  # out of [0,1]
    good_event = _event_dict(event_id="E-GOOD-1", store_id="ST_G", camera_id="CAM_G")
    fake = FakeProcessCamera()
    fake.queue_events([invalid_event, good_event])
    worker = _make_worker(db_session, fake)

    worker.run_once()

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.total_events == 2
    assert reloaded.processed_events == 2
    assert reloaded.failed_events == 1
    assert reloaded.accepted_events == 1  # the good event after the bad one was still processed


def test_ingestion_exception_records_failed_outcome_and_rolls_back(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_camera(db_session, store_id="ST_GX", camera_id="CAM_GX", video_path="data/videos/ST_GX/a.mp4")
    job = VideoProcessingService(db_session).create_job("ST_GX", "CAM_GX")

    def _boom(self, *args, **kwargs):
        raise RuntimeError("ingestion exploded")

    monkeypatch.setattr(EventIngestionService, "process_event", _boom)

    fake = FakeProcessCamera()
    fake.queue_events([_event_dict(event_id="E-BOOM-1", store_id="ST_GX", camera_id="CAM_GX")])
    worker = _make_worker(db_session, fake)

    worker.run_once()

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.failed_events == 1
    assert reloaded.total_events == 1


# ---------------------------------------------------------------------------
# H, I, J, plus the all-failed-nothing-salvaged case: terminal status
# ---------------------------------------------------------------------------


def test_successful_job_becomes_completed(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_H", camera_id="CAM_H", video_path="data/videos/ST_H/a.mp4")
    job = VideoProcessingService(db_session).create_job("ST_H", "CAM_H")

    fake = FakeProcessCamera()
    fake.queue_events([_event_dict(event_id="E-H-1", store_id="ST_H", camera_id="CAM_H")])
    worker = _make_worker(db_session, fake)

    worker.run_once()

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.status == VideoProcessingStatus.COMPLETED
    assert reloaded.completed_at is not None


def test_job_with_some_event_failures_becomes_partial(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_I", camera_id="CAM_I", video_path="data/videos/ST_I/a.mp4")
    job = VideoProcessingService(db_session).create_job("ST_I", "CAM_I")

    invalid_event = _event_dict(event_id="E-I-BAD", store_id="ST_I", camera_id="CAM_I", confidence=9.0)
    good_event = _event_dict(event_id="E-I-GOOD", store_id="ST_I", camera_id="CAM_I")
    fake = FakeProcessCamera()
    fake.queue_events([good_event, invalid_event])
    worker = _make_worker(db_session, fake)

    worker.run_once()

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.status == VideoProcessingStatus.PARTIAL
    assert reloaded.completed_at is not None


def test_job_with_all_events_failed_becomes_failed_not_partial(db_session: Session) -> None:
    """mark_partial itself refuses a run where nothing was salvaged -- the
    worker must route this case to mark_failed instead, not crash trying
    mark_partial."""
    _make_camera(db_session, store_id="ST_IF", camera_id="CAM_IF", video_path="data/videos/ST_IF/a.mp4")
    job = VideoProcessingService(db_session).create_job("ST_IF", "CAM_IF")

    invalid_event = _event_dict(event_id="E-IF-BAD", store_id="ST_IF", camera_id="CAM_IF", confidence=9.0)
    fake = FakeProcessCamera()
    fake.queue_events([invalid_event])
    worker = _make_worker(db_session, fake)

    worker.run_once()

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.status == VideoProcessingStatus.FAILED
    assert reloaded.error_message is not None


def test_catastrophic_processing_failure_becomes_failed(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_J", camera_id="CAM_J", video_path="data/videos/ST_J/a.mp4")
    job = VideoProcessingService(db_session).create_job("ST_J", "CAM_J")

    def _crash() -> Iterator[dict[str, Any]]:
        yield _event_dict(event_id="E-J-1", store_id="ST_J", camera_id="CAM_J")
        raise RuntimeError("tracker exploded mid-video")

    fake = FakeProcessCamera()
    fake.queue(_crash)
    worker = _make_worker(db_session, fake)

    worker.run_once()

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.status == VideoProcessingStatus.FAILED
    assert "tracker exploded" in reloaded.error_message
    assert reloaded.error_details_json is not None
    # The one event yielded before the crash was still durably ingested and
    # counted -- a job-level crash doesn't roll back prior per-event commits.
    assert reloaded.accepted_events == 1


def test_get_config_failure_is_treated_as_a_job_level_failure(db_session: Session) -> None:
    """A camera that can no longer produce a valid config (e.g. start_time
    cleared after the job was created) fails the job cleanly instead of
    raising out of the worker."""
    _make_camera(db_session, store_id="ST_CFGFAIL", camera_id="CAM_CFGFAIL", video_path="data/videos/ST_CFGFAIL/a.mp4")
    job = VideoProcessingService(db_session).create_job("ST_CFGFAIL", "CAM_CFGFAIL")
    camera = db_session.get(Camera, "CAM_CFGFAIL")
    camera.start_time = None
    db_session.commit()

    fake = FakeProcessCamera()
    worker = _make_worker(db_session, fake)

    worker.run_once()

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.status == VideoProcessingStatus.FAILED
    assert fake.calls == []  # process_camera was never reached


# ---------------------------------------------------------------------------
# Finalization failures (VisitorInferenceService.infer_store /
# _mark_terminal_status), which run *after* CV/event processing already
# completed, are isolated the same way a CV-level crash is -- regression
# tests for the exception-isolation gap fixed following the afff800 review.
# ---------------------------------------------------------------------------


def test_infer_store_failure_does_not_kill_the_worker(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A: VisitorInferenceService.infer_store() raising must fail only this
    job -- not escape run_once()/run_forever() -- and the worker must still
    be able to claim and finish the next job."""
    _make_camera(db_session, store_id="ST_P1", camera_id="CAM_P1", video_path="data/videos/ST_P1/a.mp4")
    _make_camera(db_session, store_id="ST_P1", camera_id="CAM_P1B", video_path="data/videos/ST_P1/b.mp4")
    job = VideoProcessingService(db_session).create_job("ST_P1", "CAM_P1")
    job.created_at = BASE_TIME
    next_job = VideoProcessingService(db_session).create_job("ST_P1", "CAM_P1B")
    next_job.created_at = BASE_TIME + timedelta(seconds=5)
    db_session.commit()

    def _boom(self, store_id):
        raise RuntimeError("inference exploded")

    monkeypatch.setattr(VisitorInferenceService, "infer_store", _boom)

    fake = FakeProcessCamera()
    fake.queue_events([_event_dict(event_id="E-P1-1", store_id="ST_P1", camera_id="CAM_P1")])
    fake.queue_events([])  # next_job: zero events -> infer_store is never called for it
    worker = _make_worker(db_session, fake)

    worker.run_once()  # must not raise even though infer_store blows up

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.status == VideoProcessingStatus.FAILED
    assert reloaded.error_message is not None
    assert "inference exploded" in reloaded.error_message
    # The event ingested before finalization failed stays durably committed --
    # a finalization failure doesn't retroactively undo prior per-event work.
    assert reloaded.accepted_events == 1

    processed = worker.run_once()
    assert processed is True
    assert db_session.get(VideoProcessingJob, next_job.id).status == VideoProcessingStatus.COMPLETED


def test_mark_terminal_status_failure_does_not_kill_the_worker(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B: an unexpected error inside _mark_terminal_status's terminal-status
    decision (here, mark_completed) must fail only this job and leave the
    worker able to process the next one."""
    _make_camera(db_session, store_id="ST_P2", camera_id="CAM_P2", video_path="data/videos/ST_P2/a.mp4")
    _make_camera(db_session, store_id="ST_P2", camera_id="CAM_P2B", video_path="data/videos/ST_P2/b.mp4")
    job = VideoProcessingService(db_session).create_job("ST_P2", "CAM_P2")
    job.created_at = BASE_TIME
    next_job = VideoProcessingService(db_session).create_job("ST_P2", "CAM_P2B")
    next_job.created_at = BASE_TIME + timedelta(seconds=5)
    db_session.commit()

    original_mark_completed = VideoProcessingService.mark_completed
    calls = {"count": 0}

    def _boom_once(self, job_arg):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("terminal status decision exploded")
        return original_mark_completed(self, job_arg)

    monkeypatch.setattr(VideoProcessingService, "mark_completed", _boom_once)

    fake = FakeProcessCamera()
    fake.queue_events([])  # zero events -> failed_events == 0 -> mark_completed is the path taken
    fake.queue_events([])
    worker = _make_worker(db_session, fake)

    worker.run_once()  # must not raise even though mark_completed blows up

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.status == VideoProcessingStatus.FAILED
    assert reloaded.error_message is not None
    assert "terminal status decision exploded" in reloaded.error_message

    processed = worker.run_once()
    assert processed is True
    assert db_session.get(VideoProcessingJob, next_job.id).status == VideoProcessingStatus.COMPLETED


def test_terminal_state_race_does_not_kill_the_worker(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C: simulates another worker's reclaim_stale_jobs resetting this job's
    status out from under this one between generator exhaustion and
    _mark_terminal_status. Both the original mark_completed attempt and the
    recovery path's mark_failed attempt then hit
    VideoProcessingService._require_running's guard -- there is no valid
    terminal-state transition left to make, so the job is left exactly where
    the (simulated) other worker put it rather than forced into an invalid
    COMPLETED/FAILED state, and neither exception may escape run_once()."""
    _make_camera(db_session, store_id="ST_P3", camera_id="CAM_P3", video_path="data/videos/ST_P3/a.mp4")
    job = VideoProcessingService(db_session).create_job("ST_P3", "CAM_P3")

    original_mark_completed = VideoProcessingService.mark_completed
    calls = {"count": 0}

    def _reclaimed_mid_finalization(self, job_arg):
        calls["count"] += 1
        if calls["count"] == 1:
            job_arg.status = VideoProcessingStatus.PENDING
            self.db.commit()
        return original_mark_completed(self, job_arg)

    monkeypatch.setattr(VideoProcessingService, "mark_completed", _reclaimed_mid_finalization)

    fake = FakeProcessCamera()
    fake.queue_events([])
    fake.queue_events([])
    worker = _make_worker(db_session, fake)

    worker.run_once()  # must not raise, even though both mark_completed and the recovery mark_failed fail

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.status == VideoProcessingStatus.PENDING

    # The worker keeps polling normally afterward -- reclaiming and
    # completing the very same job the next time round, since it's still
    # the oldest PENDING job.
    processed = worker.run_once()
    assert processed is True
    assert db_session.get(VideoProcessingJob, job.id).status == VideoProcessingStatus.COMPLETED


def test_successful_completion_with_inference_unaffected_by_finalization_wrapping(
    db_session: Session,
) -> None:
    """D: wrapping the inference + terminal-status calls in their own
    try/except must not change behavior on the ordinary success path."""
    _make_camera(db_session, store_id="ST_P5", camera_id="CAM_P5", video_path="data/videos/ST_P5/a.mp4")
    job = VideoProcessingService(db_session).create_job("ST_P5", "CAM_P5")

    fake = FakeProcessCamera()
    fake.queue_events([_event_dict(event_id="E-P5-1", store_id="ST_P5", camera_id="CAM_P5")])
    worker = _make_worker(db_session, fake)

    worker.run_once()

    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.status == VideoProcessingStatus.COMPLETED
    assert reloaded.completed_at is not None
    assert reloaded.accepted_events == 1


# ---------------------------------------------------------------------------
# M: deterministic event ids remain intact across a stale-reclaim retry
# ---------------------------------------------------------------------------


def test_deterministic_event_id_is_recognized_as_duplicate_on_retry(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_M", camera_id="CAM_M", video_path="data/videos/ST_M/a.mp4")
    service = VideoProcessingService(db_session, stale_after_minutes=10)
    service.create_job("ST_M", "CAM_M")
    fake = FakeProcessCamera()
    worker = _make_worker(db_session, fake, stale_after_minutes=10)

    first_claim = service.claim_next_job()
    same_event = _event_dict(event_id="E-RETRY-FIXED", store_id="ST_M", camera_id="CAM_M")
    accepted = worker._ingest_one_event(first_claim, same_event)
    assert accepted is True
    assert first_claim.accepted_events == 1

    # Simulate a crash: the job never reaches a terminal status, and goes stale.
    first_claim.claimed_at = first_claim.started_at - timedelta(minutes=30)
    db_session.commit()

    reclaimed = service.reclaim_stale_jobs()
    assert reclaimed[0].id == first_claim.id
    assert reclaimed[0].accepted_events == 0  # counters reset by reclaim

    second_claim = service.claim_next_job()
    assert second_claim.id == first_claim.id  # same job.id -- the retry case

    accepted_again = worker._ingest_one_event(second_claim, same_event)  # identical event_id, deterministic

    assert accepted_again is False
    assert second_claim.duplicate_events == 1
    assert second_claim.accepted_events == 0
    rows = db_session.scalars(select(Event).where(Event.source_event_id == "E-RETRY-FIXED")).all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# N: video_processing_job_id provenance
# ---------------------------------------------------------------------------


def test_video_processing_job_id_attached_to_event_and_raw_event(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_N", camera_id="CAM_N", video_path="data/videos/ST_N/a.mp4")
    job = VideoProcessingService(db_session).create_job("ST_N", "CAM_N")

    fake = FakeProcessCamera()
    fake.queue_events([_event_dict(event_id="E-N-1", store_id="ST_N", camera_id="CAM_N")])
    worker = _make_worker(db_session, fake)

    worker.run_once()

    event = db_session.scalars(select(Event).where(Event.source_event_id == "E-N-1")).one()
    raw_event = db_session.scalars(select(RawEvent).where(RawEvent.source_event_id == "E-N-1")).one()
    assert event.video_processing_job_id == job.id
    assert raw_event.video_processing_job_id == job.id
    assert raw_event.validation_status == "accepted"


# ---------------------------------------------------------------------------
# O: no open DB transaction while CV "inference" runs
# ---------------------------------------------------------------------------


def test_worker_does_not_hold_a_transaction_open_during_cv_processing(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_O", camera_id="CAM_O", video_path="data/videos/ST_O/a.mp4")
    VideoProcessingService(db_session).create_job("ST_O", "CAM_O")

    transaction_states: list[bool] = []

    def _gen() -> Iterator[dict[str, Any]]:
        # Each point control returns to this generator body corresponds to
        # the worker asking process_camera for the next frame's worth of
        # work -- exactly when real CV inference would run.
        transaction_states.append(db_session.in_transaction())
        yield _event_dict(event_id="E-O-1", store_id="ST_O", camera_id="CAM_O")
        transaction_states.append(db_session.in_transaction())
        yield _event_dict(event_id="E-O-2", store_id="ST_O", camera_id="CAM_O")
        transaction_states.append(db_session.in_transaction())

    fake = FakeProcessCamera()
    fake.queue(_gen)
    worker = _make_worker(db_session, fake)

    worker.run_once()

    assert transaction_states == [False, False, False]
