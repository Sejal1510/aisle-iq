# HTTP-layer coverage for the video-processing API (app/api/video_processing.py):
# real ASGI requests through the actual app, mirroring tests/test_replay_api.py's
# fixture pattern -- request parsing, RBAC dependency wiring, and response
# serialization. A few tests exercise VideoProcessingService.get_job/list_jobs
# directly (the store-scoping/limit/ordering guarantees the route relies on),
# the same split test_video_processing_worker.py already uses between
# worker-orchestration and service-level behavior.
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.core.security import create_access_token, create_user, grant_store_access
from app.db.session import get_db
from app.main import app
from app.models import Base
from app.models.enums import Role
from app.models.store import Camera, Store
from app.models.video_processing import VideoProcessingJob
from app.services.video_processing_service import VideoProcessingService

BASE_TIME = datetime(2026, 7, 1, 9, 0, 0)


@pytest.fixture()
def test_sessionmaker():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def db_session(test_sessionmaker) -> Session:
    session = test_sessionmaker()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(test_sessionmaker, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def override_get_db():
        db = test_sessionmaker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(main_module, "SessionLocal", test_sessionmaker)

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _auth_header(db_session: Session, role: Role, *, store_id: str, email: str) -> dict:
    if db_session.get(Store, store_id) is None:
        db_session.add(Store(id=store_id, name=None))
        db_session.commit()
    user = create_user(db_session, email=email, raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, user.id, store_id, role)
    db_session.commit()
    token, _ = create_access_token(user.id)
    return {"Authorization": f"Bearer {token}"}


def _seed_user_without_store_access(db_session: Session, email: str) -> dict:
    user = create_user(db_session, email=email, raw_password="pw")
    db_session.commit()
    token, _ = create_access_token(user.id)
    return {"Authorization": f"Bearer {token}"}


def _make_camera(
    db_session: Session,
    *,
    store_id: str,
    camera_id: str,
    video_path: str | None,
    role: str = "zone",
    start_time: datetime | None = BASE_TIME,
) -> Camera:
    if db_session.get(Store, store_id) is None:
        db_session.add(Store(id=store_id, name=None))
    camera = Camera(id=camera_id, store_id=store_id, role=role, video_path=video_path, start_time=start_time)
    db_session.add(camera)
    db_session.commit()
    return camera


# ---------------------------------------------------------------------------
# 1-3: RBAC and authentication on create
# ---------------------------------------------------------------------------


def test_create_job_requires_manager_or_above(client: TestClient, db_session: Session) -> None:
    store_id = "ST_VP_A"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_A", video_path="data/videos/ST_VP_A/a.mp4")
    analyst_headers = _auth_header(db_session, Role.ANALYST, store_id=store_id, email="analyst_a@example.com")

    response = client.post(
        f"/stores/{store_id}/video-processing", json={"camera_id": "CAM_A"}, headers=analyst_headers
    )

    assert response.status_code == 403


def test_create_job_succeeds_for_manager(client: TestClient, db_session: Session) -> None:
    store_id = "ST_VP_B"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_B", video_path="data/videos/ST_VP_B/a.mp4")
    manager_headers = _auth_header(db_session, Role.MANAGER, store_id=store_id, email="manager_b@example.com")

    response = client.post(
        f"/stores/{store_id}/video-processing", json={"camera_id": "CAM_B"}, headers=manager_headers
    )

    assert response.status_code == 201
    body = response.json()
    assert body["store_id"] == store_id
    assert body["camera_id"] == "CAM_B"
    assert body["status"] == "pending"
    assert body["total_events"] == 0


def test_create_job_requires_authentication(client: TestClient) -> None:
    response = client.post("/stores/ST_VP_C/video-processing", json={"camera_id": "CAM_C"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 4-6: request/state validation
# ---------------------------------------------------------------------------


def test_create_job_rejects_unknown_store(client: TestClient, db_session: Session) -> None:
    headers = _seed_user_without_store_access(db_session, "no_access@example.com")

    response = client.post(
        "/stores/ST_VP_UNKNOWN/video-processing", json={"camera_id": "CAM_X"}, headers=headers
    )

    # require_store_role itself 403s before the service ever runs, since the
    # caller has no StoreAccess row for a store that (from this store-scoped
    # route's perspective) may as well not exist.
    assert response.status_code == 403


def test_create_job_rejects_camera_without_video_path(client: TestClient, db_session: Session) -> None:
    store_id = "ST_VP_D"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_D", video_path=None)
    manager_headers = _auth_header(db_session, Role.MANAGER, store_id=store_id, email="manager_d@example.com")

    response = client.post(
        f"/stores/{store_id}/video-processing", json={"camera_id": "CAM_D"}, headers=manager_headers
    )

    assert response.status_code == 400


def test_create_job_rejects_camera_from_another_store(client: TestClient, db_session: Session) -> None:
    store_id = "ST_VP_E"
    other_store_id = "ST_VP_E_OTHER"
    _make_camera(db_session, store_id=other_store_id, camera_id="CAM_E_OTHER", video_path="data/videos/x.mp4")
    manager_headers = _auth_header(db_session, Role.MANAGER, store_id=store_id, email="manager_e@example.com")

    response = client.post(
        f"/stores/{store_id}/video-processing", json={"camera_id": "CAM_E_OTHER"}, headers=manager_headers
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 7: duplicate active-job creation is a safe no-op, owned entirely by
# VideoProcessingService.create_job -- the route adds no second check.
# ---------------------------------------------------------------------------


def test_duplicate_active_job_post_returns_the_same_job(client: TestClient, db_session: Session) -> None:
    store_id = "ST_VP_F"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_F", video_path="data/videos/ST_VP_F/a.mp4")
    manager_headers = _auth_header(db_session, Role.MANAGER, store_id=store_id, email="manager_f@example.com")

    first = client.post(
        f"/stores/{store_id}/video-processing", json={"camera_id": "CAM_F"}, headers=manager_headers
    )
    second = client.post(
        f"/stores/{store_id}/video-processing", json={"camera_id": "CAM_F"}, headers=manager_headers
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    rows = db_session.query(VideoProcessingJob).filter_by(camera_id="CAM_F").all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 8-11: list/get, RBAC floor, cross-store isolation
# ---------------------------------------------------------------------------


def test_list_and_get_job_allow_analyst(client: TestClient, db_session: Session) -> None:
    store_id = "ST_VP_G"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_G", video_path="data/videos/ST_VP_G/a.mp4")
    manager_headers = _auth_header(db_session, Role.MANAGER, store_id=store_id, email="manager_g@example.com")
    created = client.post(
        f"/stores/{store_id}/video-processing", json={"camera_id": "CAM_G"}, headers=manager_headers
    ).json()

    analyst_headers = _auth_header(db_session, Role.ANALYST, store_id=store_id, email="analyst_g@example.com")

    get_response = client.get(f"/stores/{store_id}/video-processing/{created['id']}", headers=analyst_headers)
    assert get_response.status_code == 200
    assert get_response.json()["id"] == created["id"]

    list_response = client.get(f"/stores/{store_id}/video-processing", headers=analyst_headers)
    assert list_response.status_code == 200
    assert any(job["id"] == created["id"] for job in list_response.json()["jobs"])


def test_get_job_from_another_store_is_not_found(client: TestClient, db_session: Session) -> None:
    store_id = "ST_VP_H"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_H", video_path="data/videos/ST_VP_H/a.mp4")
    manager_headers = _auth_header(db_session, Role.MANAGER, store_id=store_id, email="manager_h@example.com")
    created = client.post(
        f"/stores/{store_id}/video-processing", json={"camera_id": "CAM_H"}, headers=manager_headers
    ).json()

    other_store_id = "ST_VP_H_OTHER"
    other_headers = _auth_header(db_session, Role.ANALYST, store_id=other_store_id, email="other_h@example.com")

    response = client.get(f"/stores/{other_store_id}/video-processing/{created['id']}", headers=other_headers)
    assert response.status_code == 404


def test_list_jobs_is_store_scoped_and_newest_first(client: TestClient, db_session: Session) -> None:
    store_id = "ST_VP_I"
    other_store_id = "ST_VP_I_OTHER"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_I1", video_path="data/videos/ST_VP_I/1.mp4")
    _make_camera(db_session, store_id=store_id, camera_id="CAM_I2", video_path="data/videos/ST_VP_I/2.mp4")
    _make_camera(db_session, store_id=other_store_id, camera_id="CAM_I_OTHER", video_path="data/videos/x.mp4")

    manager_headers = _auth_header(db_session, Role.MANAGER, store_id=store_id, email="manager_i@example.com")
    other_manager_headers = _auth_header(
        db_session, Role.MANAGER, store_id=other_store_id, email="manager_i_other@example.com"
    )

    older = client.post(
        f"/stores/{store_id}/video-processing", json={"camera_id": "CAM_I1"}, headers=manager_headers
    ).json()
    job = db_session.get(VideoProcessingJob, older["id"])
    job.created_at = BASE_TIME
    db_session.commit()

    newer = client.post(
        f"/stores/{store_id}/video-processing", json={"camera_id": "CAM_I2"}, headers=manager_headers
    ).json()
    job = db_session.get(VideoProcessingJob, newer["id"])
    job.created_at = BASE_TIME + timedelta(seconds=5)
    db_session.commit()

    client.post(
        f"/stores/{other_store_id}/video-processing", json={"camera_id": "CAM_I_OTHER"}, headers=other_manager_headers
    )

    analyst_headers = _auth_header(db_session, Role.ANALYST, store_id=store_id, email="analyst_i@example.com")
    response = client.get(f"/stores/{store_id}/video-processing", headers=analyst_headers)

    assert response.status_code == 200
    ids = [job["id"] for job in response.json()["jobs"]]
    assert ids == [newer["id"], older["id"]]  # newest first
    assert older["id"] in ids and newer["id"] in ids
    assert all(job["store_id"] == store_id for job in response.json()["jobs"])


def test_get_nonexistent_job_is_not_found(client: TestClient, db_session: Session) -> None:
    store_id = "ST_VP_J"
    analyst_headers = _auth_header(db_session, Role.ANALYST, store_id=store_id, email="analyst_j@example.com")

    response = client.get(f"/stores/{store_id}/video-processing/does-not-exist", headers=analyst_headers)

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 12-14: response shape -- no leaked internal fields, error_details shape
# ---------------------------------------------------------------------------


def test_job_response_excludes_video_path_and_requested_by_user_id(client: TestClient, db_session: Session) -> None:
    store_id = "ST_VP_K"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_K", video_path="data/videos/ST_VP_K/a.mp4")
    manager_headers = _auth_header(db_session, Role.MANAGER, store_id=store_id, email="manager_k@example.com")

    response = client.post(
        f"/stores/{store_id}/video-processing", json={"camera_id": "CAM_K"}, headers=manager_headers
    )

    body = response.json()
    assert "video_path" not in body
    assert "requested_by_user_id" not in body


def test_job_response_serializes_dict_error_details(client: TestClient, db_session: Session) -> None:
    store_id = "ST_VP_L"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_L", video_path="data/videos/ST_VP_L/a.mp4")
    manager_headers = _auth_header(db_session, Role.MANAGER, store_id=store_id, email="manager_l@example.com")

    created = client.post(
        f"/stores/{store_id}/video-processing", json={"camera_id": "CAM_L"}, headers=manager_headers
    ).json()

    service = VideoProcessingService(db_session)
    job = db_session.get(VideoProcessingJob, created["id"])
    service.claim_next_job()  # PENDING -> RUNNING, required by mark_failed's _require_running guard
    service.mark_failed(
        job,
        error_message="Video processing failed: boom",
        error_details={"error_type": "RuntimeError"},
    )

    analyst_headers = _auth_header(db_session, Role.ANALYST, store_id=store_id, email="analyst_l@example.com")
    response = client.get(f"/stores/{store_id}/video-processing/{created['id']}", headers=analyst_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_details"] == {"error_type": "RuntimeError"}


# ---------------------------------------------------------------------------
# 15-16: VideoProcessingService.get_job/list_jobs, exercised directly
# ---------------------------------------------------------------------------


def test_service_get_job_enforces_store_scope(db_session: Session) -> None:
    _make_camera(db_session, store_id="ST_VP_M", camera_id="CAM_M", video_path="data/videos/ST_VP_M/a.mp4")
    service = VideoProcessingService(db_session)
    job = service.create_job("ST_VP_M", "CAM_M")

    assert service.get_job("ST_VP_M", job.id) is job
    assert service.get_job("ST_VP_M_OTHER", job.id) is None
    assert service.get_job("ST_VP_M", "does-not-exist") is None


def test_service_list_jobs_applies_limit_and_ordering(db_session: Session) -> None:
    store_id = "ST_VP_N"
    service = VideoProcessingService(db_session)
    created_ids: list[str] = []
    for i in range(5):
        camera_id = f"CAM_N{i}"
        _make_camera(db_session, store_id=store_id, camera_id=camera_id, video_path=f"data/videos/{camera_id}.mp4")
        job = service.create_job(store_id, camera_id)
        job.created_at = BASE_TIME + timedelta(seconds=i)
        db_session.commit()
        created_ids.append(job.id)

    limited = service.list_jobs(store_id, limit=3)

    assert len(limited) == 3
    assert [job.id for job in limited] == list(reversed(created_ids))[:3]  # newest first
