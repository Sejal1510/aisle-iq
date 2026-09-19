# P9: build_config_for_job must use VideoProcessingJob.video_path (a
# snapshot taken at job-creation time) rather than Camera.video_path (live,
# mutable -- see StoreConfigService.update_camera), so a later re-upload to
# the same camera cannot change what an already-created job processes. See
# the P9 worker-readiness audit's "video path correctness" section.
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.enums import ZoneType
from app.models.store import Camera, Store
from app.models.video_processing import VideoProcessingJob, VideoProcessingStatus
from app.services.store_config_service import PolygonGeometry, StoreConfigService
from pipeline.video.config import build_config_for_job, load_video_configs_from_db

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


def _make_job(db_session: Session, *, store_id: str, camera_id: str, video_path: str) -> VideoProcessingJob:
    job = VideoProcessingJob(
        store_id=store_id,
        camera_id=camera_id,
        video_path=video_path,
        status=VideoProcessingStatus.PENDING,
        total_events=0,
        processed_events=0,
        accepted_events=0,
        duplicate_events=0,
        failed_events=0,
    )
    db_session.add(job)
    db_session.flush()
    return job


def test_job_config_uses_the_jobs_own_video_path_snapshot(db_session: Session) -> None:
    """The exact regression the audit called for: create Job A with
    video_path=A, change Camera.video_path to B, build the config for Job A,
    assert config.video_path == A -- never B."""
    db_session.add(Store(id="ST_JOBCFG"))
    db_session.add(
        Camera(
            id="CAM_JOBCFG",
            store_id="ST_JOBCFG",
            role="zone",
            video_path="data/videos/ST_JOBCFG/original_upload.mp4",
            start_time=BASE_TIME,
        )
    )
    db_session.commit()

    job = _make_job(
        db_session,
        store_id="ST_JOBCFG",
        camera_id="CAM_JOBCFG",
        video_path="data/videos/ST_JOBCFG/original_upload.mp4",
    )
    db_session.commit()

    # A later re-upload overwrites Camera.video_path in place -- exactly
    # what StoreConfigService.update_camera already does.
    camera = db_session.get(Camera, "CAM_JOBCFG")
    camera.video_path = "data/videos/ST_JOBCFG/replacement_upload.mp4"
    db_session.commit()

    config = build_config_for_job(db_session, job)

    assert config.video_path == Path("data/videos/ST_JOBCFG/original_upload.mp4")
    assert config.video_path != Path(camera.video_path)


def test_job_config_matches_load_video_configs_for_everything_but_video_path(db_session: Session) -> None:
    """build_config_for_job must not silently diverge from
    load_video_configs_from_db on zones/entry_line/tuning fields -- only
    video_path is allowed to differ."""
    db_session.add(Store(id="ST_JOBCFG2"))
    db_session.add(
        Camera(
            id="CAM_JOBCFG2",
            store_id="ST_JOBCFG2",
            role="zone",
            video_path="data/videos/ST_JOBCFG2/live.mp4",
            start_time=BASE_TIME,
            sample_fps=5.0,
            confidence_threshold=0.5,
        )
    )
    db_session.commit()

    service = StoreConfigService(db_session)
    service.create_zone("ST_JOBCFG2", "Z_JOBCFG2", name="Shelf", zone_type=ZoneType.SHELF, is_revenue_zone=True)
    service.set_coverage(
        "CAM_JOBCFG2",
        zone_id="Z_JOBCFG2",
        geometry=PolygonGeometry(points=((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9))),
    )
    db_session.commit()

    job = _make_job(
        db_session, store_id="ST_JOBCFG2", camera_id="CAM_JOBCFG2", video_path="data/videos/ST_JOBCFG2/live.mp4"
    )
    db_session.commit()

    from_loader = load_video_configs_from_db(db_session, store_id="ST_JOBCFG2")[0]
    from_job = build_config_for_job(db_session, job)

    assert from_job.store_id == from_loader.store_id
    assert from_job.camera_id == from_loader.camera_id
    assert from_job.role == from_loader.role
    assert from_job.start_time == from_loader.start_time
    assert from_job.zones == from_loader.zones
    assert from_job.entry_line == from_loader.entry_line
    assert from_job.sample_fps == from_loader.sample_fps == 5.0
    assert from_job.confidence_threshold == from_loader.confidence_threshold == 0.5
    # The one field that's allowed (expected) to be identical here too,
    # since the job's video_path happens to match the camera's current one
    # in this test -- test_job_config_uses_the_jobs_own_video_path_snapshot
    # covers the case where they diverge.
    assert from_job.video_path == from_loader.video_path


def test_job_config_rejects_job_for_camera_in_a_different_store(db_session: Session) -> None:
    db_session.add(Store(id="ST_REAL"))
    db_session.add(
        Camera(
            id="CAM_REAL", store_id="ST_REAL", role="zone", video_path="data/videos/ST_REAL/a.mp4", start_time=BASE_TIME
        )
    )
    db_session.commit()

    job = _make_job(db_session, store_id="ST_OTHER", camera_id="CAM_REAL", video_path="data/videos/ST_REAL/a.mp4")
    db_session.commit()

    with pytest.raises(ValueError):
        build_config_for_job(db_session, job)


def test_job_config_rejects_camera_missing_start_time(db_session: Session) -> None:
    db_session.add(Store(id="ST_NOSTART"))
    db_session.add(Camera(id="CAM_NOSTART", store_id="ST_NOSTART", role="zone", video_path="anything"))
    db_session.commit()

    job = _make_job(
        db_session, store_id="ST_NOSTART", camera_id="CAM_NOSTART", video_path="data/videos/ST_NOSTART/a.mp4"
    )
    db_session.commit()

    with pytest.raises(ValueError):
        build_config_for_job(db_session, job)


def test_job_config_rejects_a_video_path_outside_videos_dir(db_session: Session) -> None:
    """Defense-in-depth: build_config_for_job re-validates containment
    itself rather than trusting the job row (see resolve_video_path)."""
    db_session.add(Store(id="ST_ESCAPE"))
    db_session.add(
        Camera(
            id="CAM_ESCAPE", store_id="ST_ESCAPE", role="zone", video_path="data/videos/ST_ESCAPE/a.mp4",
            start_time=BASE_TIME,
        )
    )
    db_session.commit()

    job = _make_job(db_session, store_id="ST_ESCAPE", camera_id="CAM_ESCAPE", video_path="../../outside.mp4")
    db_session.commit()

    with pytest.raises(ValueError):
        build_config_for_job(db_session, job)


def test_load_video_configs_from_db_still_uses_the_live_camera_video_path(db_session: Session) -> None:
    """CLI-facing behavior must be unchanged: load_video_configs_from_db
    keeps reading Camera.video_path live, not a job snapshot."""
    db_session.add(Store(id="ST_CLI"))
    db_session.add(
        Camera(
            id="CAM_CLI", store_id="ST_CLI", role="entry", video_path="data/videos/ST_CLI/first.mp4",
            start_time=BASE_TIME,
        )
    )
    db_session.commit()

    configs = load_video_configs_from_db(db_session, store_id="ST_CLI")
    assert configs[0].video_path == Path("data/videos/ST_CLI/first.mp4")

    camera = db_session.get(Camera, "CAM_CLI")
    camera.video_path = "data/videos/ST_CLI/second.mp4"
    db_session.commit()

    configs_after = load_video_configs_from_db(db_session, store_id="ST_CLI")
    assert configs_after[0].video_path == Path("data/videos/ST_CLI/second.mp4")
