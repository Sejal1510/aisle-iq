# P9: focused tests for app.services.video_processing_service.VideoProcessingService
# -- the lifecycle owner of VideoProcessingJob (creation, atomic claim, stale
# recovery, progress counters, terminal status). The future worker (not
# built yet) is the only intended caller of process_camera; nothing here
# invokes it or any CV code.
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.storage import UnsafeIdentifierError
from app.models import Base
from app.models.store import Camera, Store
from app.models.video_processing import VideoProcessingJob, VideoProcessingStatus
from app.services.video_processing_service import (
    VideoProcessingError,
    VideoProcessingService,
)

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


@pytest.fixture()
def service(db_session: Session) -> VideoProcessingService:
    # A small, fixed stale_after_minutes so reclaim tests don't depend on
    # the real Settings default.
    return VideoProcessingService(db_session, stale_after_minutes=10)


def _make_camera(
    db_session: Session,
    *,
    store_id: str,
    camera_id: str,
    video_path: str | None = "data/videos/ST/clip.mp4",
    start_time: datetime | None = BASE_TIME,
    role: str = "zone",
) -> Camera:
    if db_session.get(Store, store_id) is None:
        db_session.add(Store(id=store_id))
    camera = Camera(
        id=camera_id, store_id=store_id, role=role, video_path=video_path, start_time=start_time
    )
    db_session.add(camera)
    db_session.commit()
    return camera


# ---------------------------------------------------------------------------
# A-D, plus unknown-camera/start_time/role: create_job validation
# ---------------------------------------------------------------------------


def test_create_job_snapshots_camera_video_path(db_session: Session, service: VideoProcessingService) -> None:
    _make_camera(
        db_session, store_id="ST_A", camera_id="CAM_A", video_path="data/videos/ST_A/original.mp4"
    )

    job = service.create_job("ST_A", "CAM_A")

    assert job.video_path == "data/videos/ST_A/original.mp4"
    assert job.store_id == "ST_A"
    assert job.camera_id == "CAM_A"
    assert job.status == VideoProcessingStatus.PENDING
    assert job.claimed_at is None
    assert job.started_at is None
    assert job.completed_at is None
    assert (job.total_events, job.processed_events, job.accepted_events, job.duplicate_events, job.failed_events) == (
        0,
        0,
        0,
        0,
        0,
    )

    # A later re-upload must not retroactively change the already-created job.
    camera = db_session.get(Camera, "CAM_A")
    camera.video_path = "data/videos/ST_A/replacement.mp4"
    db_session.commit()
    reloaded = db_session.get(VideoProcessingJob, job.id)
    assert reloaded.video_path == "data/videos/ST_A/original.mp4"


def test_create_job_rejects_missing_video_path(db_session: Session, service: VideoProcessingService) -> None:
    _make_camera(db_session, store_id="ST_B", camera_id="CAM_B", video_path=None)

    with pytest.raises(VideoProcessingError):
        service.create_job("ST_B", "CAM_B")


def test_create_job_rejects_camera_missing_start_time(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_C", camera_id="CAM_C", start_time=None)

    with pytest.raises(VideoProcessingError):
        service.create_job("ST_C", "CAM_C")


def test_create_job_rejects_camera_belonging_to_another_store(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_REAL", camera_id="CAM_REAL")

    with pytest.raises(VideoProcessingError):
        service.create_job("ST_OTHER", "CAM_REAL")


def test_create_job_rejects_unknown_camera(db_session: Session, service: VideoProcessingService) -> None:
    db_session.add(Store(id="ST_NOCAM"))
    db_session.commit()

    with pytest.raises(VideoProcessingError):
        service.create_job("ST_NOCAM", "CAM_DOES_NOT_EXIST")


def test_create_job_rejects_unrecognized_camera_role(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_ROLE", camera_id="CAM_ROLE", role="not-a-real-role")

    with pytest.raises(VideoProcessingError):
        service.create_job("ST_ROLE", "CAM_ROLE")


def test_create_job_rejects_unsafe_video_path(db_session: Session, service: VideoProcessingService) -> None:
    _make_camera(
        db_session, store_id="ST_ESCAPE", camera_id="CAM_ESCAPE", video_path="data/videos/../../outside.mp4"
    )

    with pytest.raises(UnsafeIdentifierError):
        service.create_job("ST_ESCAPE", "CAM_ESCAPE")


# ---------------------------------------------------------------------------
# E: duplicate active job
# ---------------------------------------------------------------------------


def test_create_job_duplicate_pending_job_returns_the_existing_one(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_DUP", camera_id="CAM_DUP")

    first = service.create_job("ST_DUP", "CAM_DUP")
    second = service.create_job("ST_DUP", "CAM_DUP")

    assert second.id == first.id
    rows = db_session.scalars(
        select(VideoProcessingJob).where(VideoProcessingJob.camera_id == "CAM_DUP")
    ).all()
    assert len(rows) == 1


def test_create_job_duplicate_running_job_returns_the_existing_one(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_DUP2", camera_id="CAM_DUP2")

    first = service.create_job("ST_DUP2", "CAM_DUP2")
    claimed = service.claim_next_job()
    assert claimed.id == first.id

    second = service.create_job("ST_DUP2", "CAM_DUP2")

    assert second.id == first.id
    assert second.status == VideoProcessingStatus.RUNNING
    rows = db_session.scalars(
        select(VideoProcessingJob).where(VideoProcessingJob.camera_id == "CAM_DUP2")
    ).all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# F, G: atomic claim
# ---------------------------------------------------------------------------


def test_claim_next_job_transitions_pending_to_running_and_sets_timestamps(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_CLAIM", camera_id="CAM_CLAIM")
    job = service.create_job("ST_CLAIM", "CAM_CLAIM")

    claimed = service.claim_next_job()

    assert claimed.id == job.id
    assert claimed.status == VideoProcessingStatus.RUNNING
    assert claimed.claimed_at is not None
    assert claimed.started_at is not None
    assert claimed.started_at == claimed.claimed_at  # first-ever claim


def test_claim_next_job_returns_none_when_no_pending_job(
    db_session: Session, service: VideoProcessingService
) -> None:
    assert service.claim_next_job() is None


def test_claim_next_job_does_not_double_claim(db_session: Session, service: VideoProcessingService) -> None:
    _make_camera(db_session, store_id="ST_ONCE", camera_id="CAM_ONCE")
    service.create_job("ST_ONCE", "CAM_ONCE")

    first_claim = service.claim_next_job()
    second_claim = service.claim_next_job()

    assert first_claim is not None
    assert second_claim is None


def test_claim_next_job_picks_the_oldest_pending_job(db_session: Session, service: VideoProcessingService) -> None:
    _make_camera(db_session, store_id="ST_ORD", camera_id="CAM_ORD1")
    _make_camera(db_session, store_id="ST_ORD", camera_id="CAM_ORD2")
    older = service.create_job("ST_ORD", "CAM_ORD1")
    older.created_at = BASE_TIME
    newer = service.create_job("ST_ORD", "CAM_ORD2")
    newer.created_at = BASE_TIME + timedelta(seconds=5)
    db_session.commit()

    claimed = service.claim_next_job()

    assert claimed.id == older.id


# ---------------------------------------------------------------------------
# H, I: stale reclaim
# ---------------------------------------------------------------------------


def test_reclaim_stale_jobs_resets_stale_running_job_to_pending(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_STALE", camera_id="CAM_STALE")
    service.create_job("ST_STALE", "CAM_STALE")
    claimed = service.claim_next_job()
    original_started_at = claimed.started_at

    service.record_event_outcome(claimed, "accepted")
    service.record_event_outcome(claimed, "failed")
    claimed.error_message = "boom"
    claimed.error_details_json = '{"reason": "boom"}'
    claimed.claimed_at = original_started_at - timedelta(minutes=30)  # force staleness
    db_session.commit()

    reclaimed = service.reclaim_stale_jobs()

    assert len(reclaimed) == 1
    reclaimed_job = reclaimed[0]
    assert reclaimed_job.id == claimed.id
    assert reclaimed_job.status == VideoProcessingStatus.PENDING
    assert reclaimed_job.claimed_at is None
    assert reclaimed_job.started_at == original_started_at  # preserved
    assert (
        reclaimed_job.total_events,
        reclaimed_job.processed_events,
        reclaimed_job.accepted_events,
        reclaimed_job.duplicate_events,
        reclaimed_job.failed_events,
    ) == (0, 0, 0, 0, 0)
    assert reclaimed_job.error_message is None
    assert reclaimed_job.error_details_json is None


def test_reclaim_stale_jobs_does_not_touch_a_recently_claimed_running_job(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_FRESH", camera_id="CAM_FRESH")
    service.create_job("ST_FRESH", "CAM_FRESH")
    claimed = service.claim_next_job()

    reclaimed = service.reclaim_stale_jobs()

    assert reclaimed == []
    still_running = db_session.get(VideoProcessingJob, claimed.id)
    assert still_running.status == VideoProcessingStatus.RUNNING
    assert still_running.claimed_at is not None


def test_reclaim_stale_jobs_does_not_touch_pending_or_terminal_jobs(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_MIX", camera_id="CAM_MIX_DONE")
    _make_camera(db_session, store_id="ST_MIX", camera_id="CAM_MIX_PENDING")
    done_job = service.create_job("ST_MIX", "CAM_MIX_DONE")
    claimed_done = service.claim_next_job()  # only PENDING job so far -- unambiguously done_job
    assert claimed_done.id == done_job.id
    service.mark_completed(claimed_done)

    pending_job = service.create_job("ST_MIX", "CAM_MIX_PENDING")

    reclaimed = service.reclaim_stale_jobs()

    assert reclaimed == []
    assert db_session.get(VideoProcessingJob, pending_job.id).status == VideoProcessingStatus.PENDING
    assert db_session.get(VideoProcessingJob, done_job.id).status == VideoProcessingStatus.COMPLETED


def test_claim_next_job_can_reclaim_a_job_that_reclaim_stale_jobs_freed(
    db_session: Session, service: VideoProcessingService
) -> None:
    """The retry path end-to-end: stale RUNNING -> reclaim_stale_jobs ->
    PENDING -> claim_next_job -> RUNNING again, with started_at unchanged
    throughout and claimed_at bumped to the new claim time."""
    _make_camera(db_session, store_id="ST_RETRY", camera_id="CAM_RETRY")
    service.create_job("ST_RETRY", "CAM_RETRY")
    first_claim = service.claim_next_job()
    original_started_at = first_claim.started_at
    first_claim.claimed_at = original_started_at - timedelta(minutes=30)
    db_session.commit()

    service.reclaim_stale_jobs()
    second_claim = service.claim_next_job()

    assert second_claim.id == first_claim.id
    assert second_claim.status == VideoProcessingStatus.RUNNING
    assert second_claim.started_at == original_started_at
    assert second_claim.claimed_at > original_started_at


# ---------------------------------------------------------------------------
# J: progress counters
# ---------------------------------------------------------------------------


def test_record_event_outcome_updates_counters(db_session: Session, service: VideoProcessingService) -> None:
    _make_camera(db_session, store_id="ST_PROG", camera_id="CAM_PROG")
    service.create_job("ST_PROG", "CAM_PROG")
    job = service.claim_next_job()

    service.record_event_outcome(job, "accepted")
    service.record_event_outcome(job, "accepted")
    service.record_event_outcome(job, "duplicate")
    service.record_event_outcome(job, "failed")

    assert job.total_events == 4
    assert job.processed_events == 4
    assert job.accepted_events == 2
    assert job.duplicate_events == 1
    assert job.failed_events == 1


def test_record_event_outcome_rejects_unknown_outcome(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_BADOUT", camera_id="CAM_BADOUT")
    service.create_job("ST_BADOUT", "CAM_BADOUT")
    job = service.claim_next_job()

    with pytest.raises(VideoProcessingError):
        service.record_event_outcome(job, "not-a-real-outcome")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# K, N: COMPLETED semantics
# ---------------------------------------------------------------------------


def test_mark_completed_succeeds_when_no_events_failed(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_DONE", camera_id="CAM_DONE")
    service.create_job("ST_DONE", "CAM_DONE")
    job = service.claim_next_job()
    service.record_event_outcome(job, "accepted")
    service.record_event_outcome(job, "duplicate")

    completed = service.mark_completed(job)

    assert completed.status == VideoProcessingStatus.COMPLETED
    assert completed.completed_at is not None


def test_zero_event_job_can_be_marked_completed(db_session: Session, service: VideoProcessingService) -> None:
    _make_camera(db_session, store_id="ST_ZERO", camera_id="CAM_ZERO")
    service.create_job("ST_ZERO", "CAM_ZERO")
    job = service.claim_next_job()

    completed = service.mark_completed(job)

    assert completed.status == VideoProcessingStatus.COMPLETED
    assert completed.total_events == 0


def test_mark_completed_rejects_when_events_failed(db_session: Session, service: VideoProcessingService) -> None:
    _make_camera(db_session, store_id="ST_DONEFAIL", camera_id="CAM_DONEFAIL")
    service.create_job("ST_DONEFAIL", "CAM_DONEFAIL")
    job = service.claim_next_job()
    service.record_event_outcome(job, "failed")

    with pytest.raises(VideoProcessingError):
        service.mark_completed(job)


def test_mark_completed_rejects_when_job_is_not_running(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_NOTRUN", camera_id="CAM_NOTRUN")
    job = service.create_job("ST_NOTRUN", "CAM_NOTRUN")  # still PENDING, never claimed

    with pytest.raises(VideoProcessingError):
        service.mark_completed(job)


# ---------------------------------------------------------------------------
# L: PARTIAL semantics
# ---------------------------------------------------------------------------


def test_mark_partial_succeeds_with_a_mix_of_failure_and_success(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_PART", camera_id="CAM_PART")
    service.create_job("ST_PART", "CAM_PART")
    job = service.claim_next_job()
    service.record_event_outcome(job, "accepted")
    service.record_event_outcome(job, "failed")

    partial = service.mark_partial(job)

    assert partial.status == VideoProcessingStatus.PARTIAL
    assert partial.completed_at is not None


def test_mark_partial_rejects_when_nothing_failed(db_session: Session, service: VideoProcessingService) -> None:
    _make_camera(db_session, store_id="ST_PARTNOFAIL", camera_id="CAM_PARTNOFAIL")
    service.create_job("ST_PARTNOFAIL", "CAM_PARTNOFAIL")
    job = service.claim_next_job()
    service.record_event_outcome(job, "accepted")

    with pytest.raises(VideoProcessingError):
        service.mark_partial(job)


def test_mark_partial_rejects_when_nothing_was_salvaged(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(db_session, store_id="ST_PARTALLFAIL", camera_id="CAM_PARTALLFAIL")
    service.create_job("ST_PARTALLFAIL", "CAM_PARTALLFAIL")
    job = service.claim_next_job()
    service.record_event_outcome(job, "failed")
    service.record_event_outcome(job, "failed")

    with pytest.raises(VideoProcessingError):
        service.mark_partial(job)


# ---------------------------------------------------------------------------
# M: FAILED semantics
# ---------------------------------------------------------------------------


def test_mark_failed_succeeds_regardless_of_counters(db_session: Session, service: VideoProcessingService) -> None:
    _make_camera(db_session, store_id="ST_CRASH", camera_id="CAM_CRASH")
    service.create_job("ST_CRASH", "CAM_CRASH")
    job = service.claim_next_job()  # crashed before yielding anything at all

    failed = service.mark_failed(
        job, error_message="tracker crashed", error_details={"stage": "read_video_metadata"}
    )

    assert failed.status == VideoProcessingStatus.FAILED
    assert failed.error_message == "tracker crashed"
    assert failed.error_details_json == '{"stage": "read_video_metadata"}'
    assert failed.completed_at is not None


def test_mark_failed_rejects_when_job_is_not_running(db_session: Session, service: VideoProcessingService) -> None:
    _make_camera(db_session, store_id="ST_CRASHNOTRUN", camera_id="CAM_CRASHNOTRUN")
    job = service.create_job("ST_CRASHNOTRUN", "CAM_CRASHNOTRUN")  # still PENDING

    with pytest.raises(VideoProcessingError):
        service.mark_failed(job, error_message="should not be reachable")


# ---------------------------------------------------------------------------
# O: job-scoped config
# ---------------------------------------------------------------------------


def test_get_config_uses_the_jobs_own_video_path_after_camera_video_path_changes(
    db_session: Session, service: VideoProcessingService
) -> None:
    _make_camera(
        db_session, store_id="ST_CFG", camera_id="CAM_CFG", video_path="data/videos/ST_CFG/original.mp4"
    )
    job = service.create_job("ST_CFG", "CAM_CFG")

    camera = db_session.get(Camera, "CAM_CFG")
    camera.video_path = "data/videos/ST_CFG/replacement.mp4"
    db_session.commit()

    config = service.get_config(job)

    assert config.video_path == Path("data/videos/ST_CFG/original.mp4")
