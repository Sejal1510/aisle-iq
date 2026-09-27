# P9 dashboard panel: HTTP-layer contract test proving the exact two-step
# flow the new dashboard panel performs actually works end to end through
# real endpoints -- upload_camera_video (app/api/onboarding.py) followed by
# create_video_processing_job (app/api/video_processing.py). These two
# routes already have extensive coverage individually (tests/
# test_onboarding_api.py's video_path-as-a-string-field tests,
# tests/test_video_processing_api.py's job-creation RBAC/duplicate/scoping
# tests), but nothing previously exercised the real multipart upload
# endpoint over HTTP or chained it into a job creation call -- see the P9
# completion audit's ยง1/ยง2. Mirrors tests/test_video_processing_api.py's own
# fixture pattern (itself mirroring tests/test_replay_api.py's).
from __future__ import annotations

import io
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.core.security import create_access_token, create_user, grant_store_access
from app.core.storage import PROJECT_ROOT
from app.db.session import get_db
from app.main import app
from app.models import Base
from app.models.enums import Role
from app.models.store import Store

STORE_ID = "ST_P9_PANEL"
FAKE_VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42fake-video-bytes-for-testing"


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


def _auth_header(db_session: Session, role: Role, *, email: str) -> dict:
    if db_session.get(Store, STORE_ID) is None:
        db_session.add(Store(id=STORE_ID, name=None))
        db_session.commit()
    user = create_user(db_session, email=email, raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, user.id, STORE_ID, role)
    db_session.commit()
    token, _ = create_access_token(user.id)
    return {"Authorization": f"Bearer {token}"}


def _create_camera(client: TestClient, admin_headers: dict, camera_id: str, *, start_time: bool = True) -> None:
    payload = {"camera_id": camera_id, "role": "entry"}
    if start_time:
        payload["start_time"] = datetime(2026, 7, 1, 9, 0, 0).isoformat()
    response = client.post(f"/stores/{STORE_ID}/config/cameras", json=payload, headers=admin_headers)
    assert response.status_code == 201, response.text


def _upload_video(client: TestClient, headers: dict, camera_id: str):
    return client.post(
        f"/stores/{STORE_ID}/config/cameras/{camera_id}/video",
        files={"file": ("clip.mp4", io.BytesIO(FAKE_VIDEO_BYTES), "video/mp4")},
        headers=headers,
    )


def test_upload_then_create_job_is_a_working_end_to_end_contract(
    client: TestClient, db_session: Session
) -> None:
    """The exact sequence the dashboard's Video Processing panel performs:
    pick a camera, upload a file, create a job for it -- and the uploaded
    bytes are genuinely readable back from disk at the path the API returned,
    not just a database row with an unopenable path."""
    admin_headers = _auth_header(db_session, Role.ADMIN, email="admin@p9panel.local")
    manager_headers = _auth_header(db_session, Role.MANAGER, email="manager@p9panel.local")
    _create_camera(client, admin_headers, "CAM_PANEL_1")

    upload_response = _upload_video(client, admin_headers, "CAM_PANEL_1")
    assert upload_response.status_code == 201, upload_response.text
    camera_out = upload_response.json()
    video_path = camera_out["video_path"]
    assert video_path.startswith(f"data/videos/{STORE_ID}/")
    assert video_path.endswith(".mp4")

    # The relative path the API returned must resolve to a real file on disk
    # containing exactly the bytes that were uploaded -- proving save_upload
    # actually persisted them where VideoProcessingService.create_job (and
    # later, the worker's pipeline.video.tracking.UltralyticsByteTracker)
    # will look for them, not just that the DB column got set.
    saved_file = PROJECT_ROOT / video_path
    assert saved_file.is_file()
    assert saved_file.read_bytes() == FAKE_VIDEO_BYTES

    job_response = client.post(
        f"/stores/{STORE_ID}/video-processing",
        json={"camera_id": "CAM_PANEL_1"},
        headers=manager_headers,
    )
    assert job_response.status_code == 201, job_response.text
    job = job_response.json()
    assert job["status"] == "pending"
    assert job["camera_id"] == "CAM_PANEL_1"
    assert job["store_id"] == STORE_ID
    # VideoProcessingJobOut deliberately never echoes video_path back (see
    # tests/test_video_processing_api.py's
    # test_job_response_excludes_video_path_and_requested_by_user_id) -- the
    # job's own snapshot of it is verified indirectly, through
    # VideoProcessingService.create_job's own unit tests.
    assert "video_path" not in job

    saved_file.unlink(missing_ok=True)


def test_video_upload_is_admin_gated_while_job_creation_is_manager_gated(
    client: TestClient, db_session: Session
) -> None:
    """Proves the two-tier RBAC boundary the dashboard's two buttons (upload
    vs. create job) actually rely on: a MANAGER can queue processing for an
    already-uploaded video but cannot themselves upload one -- structural
    camera config stays ADMIN-only (app/api/onboarding.py), starting
    processing is a MANAGER-level operational action
    (app/api/video_processing.py)."""
    admin_headers = _auth_header(db_session, Role.ADMIN, email="admin2@p9panel.local")
    manager_headers = _auth_header(db_session, Role.MANAGER, email="manager2@p9panel.local")
    _create_camera(client, admin_headers, "CAM_PANEL_2")

    manager_upload = _upload_video(client, manager_headers, "CAM_PANEL_2")
    assert manager_upload.status_code == 403

    admin_upload = _upload_video(client, admin_headers, "CAM_PANEL_2")
    assert admin_upload.status_code == 201, admin_upload.text
    saved_file = PROJECT_ROOT / admin_upload.json()["video_path"]

    manager_job = client.post(
        f"/stores/{STORE_ID}/video-processing",
        json={"camera_id": "CAM_PANEL_2"},
        headers=manager_headers,
    )
    assert manager_job.status_code == 201, manager_job.text

    saved_file.unlink(missing_ok=True)


def test_job_creation_gives_a_clear_actionable_error_when_camera_has_no_start_time(
    client: TestClient, db_session: Session
) -> None:
    """A video can be uploaded before onboarding (start_time) is finished --
    upload_camera_video only ever touches video_path. Creating a job at that
    point must fail with a specific, dashboard-surfaceable message (the
    postJson/postForm helpers in dashboard/app.js render `detail` verbatim),
    not a generic 500 or a silent no-op."""
    admin_headers = _auth_header(db_session, Role.ADMIN, email="admin3@p9panel.local")
    _create_camera(client, admin_headers, "CAM_PANEL_3", start_time=False)
    upload_response = _upload_video(client, admin_headers, "CAM_PANEL_3")
    assert upload_response.status_code == 201, upload_response.text
    saved_file = PROJECT_ROOT / upload_response.json()["video_path"]

    job_response = client.post(
        f"/stores/{STORE_ID}/video-processing",
        json={"camera_id": "CAM_PANEL_3"},
        headers=admin_headers,
    )
    assert job_response.status_code == 400
    assert "start_time" in job_response.json()["detail"]

    saved_file.unlink(missing_ok=True)
