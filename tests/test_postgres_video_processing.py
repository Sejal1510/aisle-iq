# P9: verifies VideoProcessingService's atomic claim/reclaim SQL (a Core
# UPDATE ... WHERE id = (SELECT ... LIMIT 1) ... RETURNING statement, plus
# the video_processing_job.camera_id partial unique index) against a real
# PostgreSQL server, not just SQLite -- see app/services/video_processing_service.py's
# claim_next_job/reclaim_stale_jobs docstrings for why this needs checking on
# both dialects. Opt-in via AISLEIQ_TEST_POSTGRES_URL -- skipped entirely
# otherwise, same mechanism tests/test_postgres_smoke.py already uses.
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.store import Camera, Store
from app.models.video_processing import VideoProcessingStatus
from app.services.video_processing_service import VideoProcessingService

POSTGRES_URL = os.environ.get("AISLEIQ_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="AISLEIQ_TEST_POSTGRES_URL not set -- PostgreSQL verification is opt-in",
)

BASE_TIME = datetime(2026, 7, 1, 9, 0, 0)


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(POSTGRES_URL)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        # See test_postgres_smoke.py's own fixture for why drop_all (not
        # Alembic downgrade) is what leaves the database empty for the next run.
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _make_camera(db_session: Session, *, store_id: str, camera_id: str, video_path: str) -> None:
    db_session.add(Store(id=store_id))
    db_session.add(Camera(id=camera_id, store_id=store_id, role="zone", video_path=video_path, start_time=BASE_TIME))
    db_session.commit()


def test_claim_next_job_against_postgres(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_PG_CLAIM", camera_id="CAM_PG_CLAIM", video_path="data/videos/ST_PG_CLAIM/a.mp4")
    service = VideoProcessingService(db_session, stale_after_minutes=10)
    job = service.create_job("ST_PG_CLAIM", "CAM_PG_CLAIM")

    claimed = service.claim_next_job()

    assert claimed.id == job.id
    assert claimed.status == VideoProcessingStatus.RUNNING
    assert claimed.claimed_at is not None
    assert claimed.started_at == claimed.claimed_at
    assert service.claim_next_job() is None


def test_reclaim_stale_jobs_against_postgres(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_PG_STALE", camera_id="CAM_PG_STALE", video_path="data/videos/ST_PG_STALE/a.mp4")
    service = VideoProcessingService(db_session, stale_after_minutes=10)
    service.create_job("ST_PG_STALE", "CAM_PG_STALE")
    claimed = service.claim_next_job()
    original_started_at = claimed.started_at
    service.record_event_outcome(claimed, "accepted")
    claimed.claimed_at = original_started_at - timedelta(minutes=30)
    db_session.commit()

    reclaimed = service.reclaim_stale_jobs()

    assert len(reclaimed) == 1
    assert reclaimed[0].status == VideoProcessingStatus.PENDING
    assert reclaimed[0].claimed_at is None
    assert reclaimed[0].started_at == original_started_at
    assert reclaimed[0].accepted_events == 0

    re_claimed = service.claim_next_job()
    assert re_claimed.id == claimed.id
    assert re_claimed.started_at == original_started_at


def test_duplicate_active_job_enforced_by_postgres_partial_unique_index(db_session: Session) -> None:
    """The partial unique index (video_processing_job.camera_id, WHERE status
    IN ('PENDING','RUNNING')) must actually be enforced by a real PostgreSQL
    server -- this is what create_job's IntegrityError handling depends on."""
    _make_camera(db_session, store_id="ST_PG_DUP", camera_id="CAM_PG_DUP", video_path="data/videos/ST_PG_DUP/a.mp4")
    service = VideoProcessingService(db_session, stale_after_minutes=10)

    first = service.create_job("ST_PG_DUP", "CAM_PG_DUP")
    second = service.create_job("ST_PG_DUP", "CAM_PG_DUP")

    assert second.id == first.id
