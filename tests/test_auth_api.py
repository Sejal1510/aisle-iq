# P4.4: HTTP-layer tests through the real ASGI application (matching
# tests/test_api_integration.py's convention) for login, /auth/me, the
# ADMIN-only store-access grant endpoint, and RBAC gating on the existing
# analytics routes. Also guards that ingestion's ApiKey auth is unaffected.
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.core.security import (
    create_access_token,
    create_api_key,
    create_user,
    grant_store_access,
)
from app.db.session import get_db
from app.main import app
from app.models import Base
from app.models.enums import Role
from app.models.store import Store


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


@pytest.fixture()
def analyst_user(db_session: Session) -> dict:
    if db_session.get(Store, "ST_AUTH") is None:
        db_session.add(Store(id="ST_AUTH", name=None))
    user = create_user(db_session, email="analyst@aisleiq.local", raw_password="pw-analyst")
    db_session.commit()
    grant_store_access(db_session, user.id, "ST_AUTH", Role.ANALYST)
    db_session.commit()
    return {"user_id": user.id, "email": user.email}


@pytest.fixture()
def admin_user(db_session: Session) -> dict:
    if db_session.get(Store, "ST_AUTH") is None:
        db_session.add(Store(id="ST_AUTH", name=None))
    user = create_user(db_session, email="admin@aisleiq.local", raw_password="pw-admin")
    db_session.commit()
    grant_store_access(db_session, user.id, "ST_AUTH", Role.ADMIN)
    db_session.commit()
    return {"user_id": user.id, "email": user.email}


def test_login_succeeds_with_correct_credentials(client: TestClient, analyst_user: dict) -> None:
    response = client.post("/auth/login", json={"email": analyst_user["email"], "password": "pw-analyst"})

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] > 0


def test_login_rejects_wrong_password(client: TestClient, analyst_user: dict) -> None:
    response = client.post("/auth/login", json={"email": analyst_user["email"], "password": "wrong"})
    assert response.status_code == 401


def test_login_rejects_unknown_email(client: TestClient) -> None:
    response = client.post("/auth/login", json={"email": "nobody@example.com", "password": "whatever"})
    assert response.status_code == 401


def test_login_rejects_inactive_user(client: TestClient, db_session: Session) -> None:
    user = create_user(db_session, email="inactive@example.com", raw_password="pw")
    user.is_active = False
    db_session.commit()

    response = client.post("/auth/login", json={"email": "inactive@example.com", "password": "pw"})
    assert response.status_code == 401


def test_me_returns_current_user_and_store_grants(client: TestClient, analyst_user: dict) -> None:
    login_response = client.post("/auth/login", json={"email": analyst_user["email"], "password": "pw-analyst"})
    token = login_response.json()["access_token"]

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == analyst_user["email"]
    assert body["store_access"] == [{"store_id": "ST_AUTH", "role": "analyst"}]


def test_me_requires_authentication(client: TestClient) -> None:
    response = client.get("/auth/me")
    assert response.status_code == 401


def test_protected_analytics_route_requires_authentication(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id="ST_AUTH", name=None))
    db_session.commit()

    response = client.get("/stores/ST_AUTH/metrics")

    assert response.status_code == 401


def test_protected_analytics_route_rejects_user_without_store_access(
    client: TestClient, db_session: Session
) -> None:
    db_session.add(Store(id="ST_AUTH", name=None))
    user = create_user(db_session, email="outsider@example.com", raw_password="pw")
    db_session.commit()
    token, _ = create_access_token(user.id)

    response = client.get("/stores/ST_AUTH/metrics", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403


def test_protected_analytics_route_allows_granted_user(client: TestClient, analyst_user: dict) -> None:
    token, _ = create_access_token(analyst_user["user_id"])

    response = client.get("/stores/ST_AUTH/metrics", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["store_id"] == "ST_AUTH"


def test_grant_access_requires_admin_role(client: TestClient, analyst_user: dict) -> None:
    token, _ = create_access_token(analyst_user["user_id"])

    response = client.post(
        "/stores/ST_AUTH/access",
        json={"email": "someone@example.com", "role": "analyst"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


def test_grant_access_requires_authentication(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id="ST_AUTH", name=None))
    db_session.commit()

    response = client.post("/stores/ST_AUTH/access", json={"email": "someone@example.com", "role": "analyst"})

    assert response.status_code == 401


def test_grant_access_succeeds_for_admin_and_is_visible_via_me(
    client: TestClient, admin_user: dict, db_session: Session
) -> None:
    create_user(db_session, email="new-analyst@example.com", raw_password="pw-new")
    db_session.commit()
    admin_token, _ = create_access_token(admin_user["user_id"])

    grant_response = client.post(
        "/stores/ST_AUTH/access",
        json={"email": "new-analyst@example.com", "role": "manager"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert grant_response.status_code == 201
    assert grant_response.json() == {"store_id": "ST_AUTH", "email": "new-analyst@example.com", "role": "manager"}

    login_response = client.post("/auth/login", json={"email": "new-analyst@example.com", "password": "pw-new"})
    me_response = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {login_response.json()['access_token']}"}
    )
    assert me_response.json()["store_access"] == [{"store_id": "ST_AUTH", "role": "manager"}]


def test_grant_access_returns_404_for_unknown_email(client: TestClient, admin_user: dict) -> None:
    token, _ = create_access_token(admin_user["user_id"])

    response = client.post(
        "/stores/ST_AUTH/access",
        json={"email": "ghost@example.com", "role": "analyst"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


def test_regranting_updates_role_instead_of_creating_duplicate(
    client: TestClient, admin_user: dict, db_session: Session
) -> None:
    create_user(db_session, email="flexible@example.com", raw_password="pw")
    db_session.commit()
    token, _ = create_access_token(admin_user["user_id"])
    grant_body = {"email": "flexible@example.com", "role": "analyst"}

    first = client.post("/stores/ST_AUTH/access", json=grant_body, headers={"Authorization": f"Bearer {token}"})
    assert first.status_code == 201

    grant_body["role"] = "admin"
    second = client.post("/stores/ST_AUTH/access", json=grant_body, headers={"Authorization": f"Bearer {token}"})
    assert second.status_code == 201
    assert second.json()["role"] == "admin"


def test_ingestion_api_key_auth_is_unaffected_by_p44(client: TestClient, db_session: Session) -> None:
    """Ingestion keeps using X-API-Key, a separate credential system from the
    new bearer-token auth -- regression guard that P4.4 didn't touch it."""
    db_session.add(Store(id="ST_INGEST", name=None))
    db_session.flush()
    _row, raw_key = create_api_key(db_session, "ST_INGEST")
    db_session.commit()

    response = client.post(
        "/events/",
        json={
            "event_id": "evt-p44-1",
            "event_type": "entry",
            "id_token": "ID_P44_1",
            "store_code": "ST_INGEST",
            "camera_id": "CAM_ENTRY_1",
            "event_timestamp": "2026-06-01T09:00:00",
        },
        headers={"X-API-Key": raw_key},
    )

    assert response.status_code == 202
