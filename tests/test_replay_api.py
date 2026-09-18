# HTTP-layer coverage for the replay API (app/api/replay.py): real ASGI
# requests through the actual app, mirroring tests/test_api_integration.py's
# fixture pattern -- request parsing, RBAC dependency wiring, and response
# serialization, not just direct service calls (see test_replay_service.py
# for those).
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
from app.models.store import Store

STORE_ID = "ST_REPLAY_HTTP"


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


def _auth_header(db_session: Session, role: Role, *, store_id: str = STORE_ID, email: str = "replay@example.com") -> dict:
    if db_session.get(Store, store_id) is None:
        db_session.add(Store(id=store_id, name=None))
    user = create_user(db_session, email=email, raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, user.id, store_id, role)
    db_session.commit()
    token, _ = create_access_token(user.id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def seed_live_event(db_session: Session):
    from app.core.security import create_api_key
    from app.schemas.event import EntryEvent
    from app.services.event_ingestion_service import EventIngestionService

    if db_session.get(Store, STORE_ID) is None:
        db_session.add(Store(id=STORE_ID, name=None))
        db_session.flush()
    create_api_key(db_session, STORE_ID)
    db_session.commit()

    payload = EntryEvent(
        id_token="ID_HTTP_REPLAY_1",
        store_code=STORE_ID,
        camera_id="CAM_ENTRY_1",
        event_timestamp="2026-06-01T09:00:00",
    )
    event = EventIngestionService(db_session).process_event(payload)
    db_session.commit()
    return event


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_create_replay_requires_manager_or_above(client: TestClient, db_session: Session, seed_live_event) -> None:
    analyst_headers = _auth_header(db_session, Role.ANALYST, email="analyst@example.com")

    response = client.post(
        f"/stores/{STORE_ID}/replay",
        json={"source_type": "raw_event_archive"},
        headers=analyst_headers,
    )

    assert response.status_code == 403


def test_create_replay_succeeds_for_manager(client: TestClient, db_session: Session, seed_live_event) -> None:
    manager_headers = _auth_header(db_session, Role.MANAGER, email="manager@example.com")

    response = client.post(
        f"/stores/{STORE_ID}/replay",
        json={"source_type": "raw_event_archive"},
        headers=manager_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "completed"
    assert body["duplicate_events"] == 1
    assert body["accepted_events"] == 0


def test_create_replay_requires_authentication(client: TestClient) -> None:
    response = client.post(f"/stores/{STORE_ID}/replay", json={"source_type": "raw_event_archive"})
    assert response.status_code == 401


def test_get_and_list_replay_allow_analyst(client: TestClient, db_session: Session, seed_live_event) -> None:
    manager_headers = _auth_header(db_session, Role.MANAGER, email="manager2@example.com")
    created = client.post(
        f"/stores/{STORE_ID}/replay",
        json={"source_type": "raw_event_archive"},
        headers=manager_headers,
    ).json()

    analyst_headers = _auth_header(db_session, Role.ANALYST, email="analyst2@example.com")

    get_response = client.get(f"/stores/{STORE_ID}/replay/{created['id']}", headers=analyst_headers)
    assert get_response.status_code == 200
    assert get_response.json()["id"] == created["id"]

    list_response = client.get(f"/stores/{STORE_ID}/replay", headers=analyst_headers)
    assert list_response.status_code == 200
    assert any(job["id"] == created["id"] for job in list_response.json()["jobs"])


def test_get_replay_job_from_another_store_is_not_found(client: TestClient, db_session: Session, seed_live_event) -> None:
    manager_headers = _auth_header(db_session, Role.MANAGER, email="manager3@example.com")
    created = client.post(
        f"/stores/{STORE_ID}/replay",
        json={"source_type": "raw_event_archive"},
        headers=manager_headers,
    ).json()

    other_store_id = "ST_REPLAY_HTTP_OTHER"
    other_headers = _auth_header(db_session, Role.ANALYST, store_id=other_store_id, email="other@example.com")

    response = client.get(f"/stores/{other_store_id}/replay/{created['id']}", headers=other_headers)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_create_replay_rejects_unknown_store(client: TestClient, db_session: Session) -> None:
    user = _seed_manager(db_session, "unknownstore@example.com")

    response = client.post(
        f"/stores/{STORE_ID}/replay",
        json={"source_type": "raw_event_archive"},
        headers=user,
    )

    # require_store_role itself 403s before the service ever runs, since the
    # caller has no StoreAccess row for a store that (from this store-scoped
    # route's perspective) may as well not exist.
    assert response.status_code == 403


def test_create_replay_rejects_jsonl_without_source_ref(
    client: TestClient, db_session: Session, seed_live_event
) -> None:
    manager_headers = _auth_header(db_session, Role.MANAGER, email="manager4@example.com")

    response = client.post(
        f"/stores/{STORE_ID}/replay",
        json={"source_type": "jsonl_file"},
        headers=manager_headers,
    )

    assert response.status_code == 400


def _seed_manager(db_session: Session, email: str) -> dict:
    user = create_user(db_session, email=email, raw_password="pw")
    db_session.commit()
    token, _ = create_access_token(user.id)
    return {"Authorization": f"Bearer {token}"}
