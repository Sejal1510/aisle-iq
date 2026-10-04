# HTTP-layer coverage for the P10.1 identity-linking API
# (app/api/identity_linking.py): real ASGI requests through the actual app,
# mirroring tests/test_video_processing_api.py's fixture pattern -- RBAC
# dependency wiring, request validation, and response serialization.
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
from app.models.enums import EventType, Role
from app.models.event import Event
from app.models.store import Camera, Store
from app.models.tracking import TrackedEntity, VisitSession

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


def _make_camera(db_session: Session, *, store_id: str, camera_id: str, role: str = "zone") -> Camera:
    if db_session.get(Store, store_id) is None:
        db_session.add(Store(id=store_id, name=None))
    camera = Camera(id=camera_id, store_id=store_id, role=role)
    db_session.add(camera)
    db_session.commit()
    return camera


def _make_entity_with_event(
    db_session: Session, *, store_id: str, entity_id: str, camera_id: str, ts: datetime
) -> None:
    if db_session.get(TrackedEntity, entity_id) is None:
        db_session.add(TrackedEntity(id=entity_id, store_id=store_id, is_staff=False))
    session = VisitSession(tracked_entity_id=entity_id, store_id=store_id, entry_time=ts)
    db_session.add(session)
    db_session.flush()
    db_session.add(
        Event(
            session_id=session.id,
            tracked_entity_id=entity_id,
            store_id=store_id,
            camera_id=camera_id,
            event_type=EventType.ZONE_ENTERED,
            timestamp=ts,
        )
    )
    db_session.commit()


# ---------------------------------------------------------------------------
# CameraAdjacency: RBAC + validation
# ---------------------------------------------------------------------------


def test_create_camera_adjacency_requires_admin(client: TestClient, db_session: Session) -> None:
    store_id = "ST_LNK_A"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_A1", role="entry")
    _make_camera(db_session, store_id=store_id, camera_id="CAM_A2", role="zone")
    manager_headers = _auth_header(db_session, Role.MANAGER, store_id=store_id, email="manager_a@example.com")

    response = client.post(
        f"/stores/{store_id}/camera-adjacency",
        json={
            "from_camera_id": "CAM_A1",
            "to_camera_id": "CAM_A2",
            "min_transit_seconds": 60,
            "max_transit_seconds": 180,
        },
        headers=manager_headers,
    )

    assert response.status_code == 403


def test_create_camera_adjacency_succeeds_for_admin(client: TestClient, db_session: Session) -> None:
    store_id = "ST_LNK_B"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_B1", role="entry")
    _make_camera(db_session, store_id=store_id, camera_id="CAM_B2", role="zone")
    admin_headers = _auth_header(db_session, Role.ADMIN, store_id=store_id, email="admin_b@example.com")

    response = client.post(
        f"/stores/{store_id}/camera-adjacency",
        json={
            "from_camera_id": "CAM_B1",
            "to_camera_id": "CAM_B2",
            "min_transit_seconds": 60,
            "max_transit_seconds": 180,
        },
        headers=admin_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["store_id"] == store_id
    assert body["from_camera_id"] == "CAM_B1"
    assert body["to_camera_id"] == "CAM_B2"
    assert body["min_transit_seconds"] == 60
    assert body["max_transit_seconds"] == 180


def test_create_camera_adjacency_requires_authentication(client: TestClient) -> None:
    response = client.post(
        "/stores/ST_LNK_C/camera-adjacency",
        json={"from_camera_id": "CAM_C1", "to_camera_id": "CAM_C2", "min_transit_seconds": 0, "max_transit_seconds": 60},
    )
    assert response.status_code == 401


def test_create_camera_adjacency_rejects_unknown_camera(client: TestClient, db_session: Session) -> None:
    store_id = "ST_LNK_D"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_D1", role="entry")
    admin_headers = _auth_header(db_session, Role.ADMIN, store_id=store_id, email="admin_d@example.com")

    response = client.post(
        f"/stores/{store_id}/camera-adjacency",
        json={
            "from_camera_id": "CAM_D1",
            "to_camera_id": "CAM_DOES_NOT_EXIST",
            "min_transit_seconds": 60,
            "max_transit_seconds": 180,
        },
        headers=admin_headers,
    )

    assert response.status_code == 400


def test_create_camera_adjacency_rejects_inverted_window(client: TestClient, db_session: Session) -> None:
    store_id = "ST_LNK_E"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_E1", role="entry")
    _make_camera(db_session, store_id=store_id, camera_id="CAM_E2", role="zone")
    admin_headers = _auth_header(db_session, Role.ADMIN, store_id=store_id, email="admin_e@example.com")

    response = client.post(
        f"/stores/{store_id}/camera-adjacency",
        json={
            "from_camera_id": "CAM_E1",
            "to_camera_id": "CAM_E2",
            "min_transit_seconds": 180,
            "max_transit_seconds": 60,
        },
        headers=admin_headers,
    )

    assert response.status_code == 422


def test_list_camera_adjacency_allows_analyst(client: TestClient, db_session: Session) -> None:
    store_id = "ST_LNK_F"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_F1", role="entry")
    _make_camera(db_session, store_id=store_id, camera_id="CAM_F2", role="zone")
    admin_headers = _auth_header(db_session, Role.ADMIN, store_id=store_id, email="admin_f@example.com")
    client.post(
        f"/stores/{store_id}/camera-adjacency",
        json={
            "from_camera_id": "CAM_F1",
            "to_camera_id": "CAM_F2",
            "min_transit_seconds": 60,
            "max_transit_seconds": 180,
        },
        headers=admin_headers,
    )

    analyst_headers = _auth_header(db_session, Role.ANALYST, store_id=store_id, email="analyst_f@example.com")
    response = client.get(f"/stores/{store_id}/camera-adjacency", headers=analyst_headers)

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["from_camera_id"] == "CAM_F1"


# ---------------------------------------------------------------------------
# Identity-link-candidates: RBAC + behavior
# ---------------------------------------------------------------------------


def test_evaluate_candidates_requires_manager_or_above(client: TestClient, db_session: Session) -> None:
    store_id = "ST_LNK_G"
    analyst_headers = _auth_header(db_session, Role.ANALYST, store_id=store_id, email="analyst_g@example.com")

    response = client.post(f"/stores/{store_id}/identity-link-candidates", headers=analyst_headers)

    assert response.status_code == 403


def test_evaluate_candidates_succeeds_for_manager(client: TestClient, db_session: Session) -> None:
    store_id = "ST_LNK_H"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_H1", role="entry")
    _make_camera(db_session, store_id=store_id, camera_id="CAM_H2", role="zone")
    admin_headers = _auth_header(db_session, Role.ADMIN, store_id=store_id, email="admin_h@example.com")
    client.post(
        f"/stores/{store_id}/camera-adjacency",
        json={
            "from_camera_id": "CAM_H1",
            "to_camera_id": "CAM_H2",
            "min_transit_seconds": 60,
            "max_transit_seconds": 180,
        },
        headers=admin_headers,
    )
    _make_entity_with_event(db_session, store_id=store_id, entity_id="ENT_H1", camera_id="CAM_H1", ts=BASE_TIME)
    _make_entity_with_event(
        db_session, store_id=store_id, entity_id="ENT_H2", camera_id="CAM_H2", ts=BASE_TIME + timedelta(seconds=120)
    )

    manager_headers = _auth_header(db_session, Role.MANAGER, store_id=store_id, email="manager_h@example.com")
    response = client.post(f"/stores/{store_id}/identity-link-candidates", headers=manager_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["store_id"] == store_id
    assert body["accepted"] == 1
    assert body["ambiguous"] == 0
    assert body["below_threshold"] == 0
    assert body["total"] == 1


def test_evaluate_candidates_requires_authentication(client: TestClient) -> None:
    response = client.post("/stores/ST_LNK_I/identity-link-candidates")
    assert response.status_code == 401


def test_list_candidates_allows_analyst_and_filters_by_accepted(client: TestClient, db_session: Session) -> None:
    store_id = "ST_LNK_J"
    _make_camera(db_session, store_id=store_id, camera_id="CAM_J1", role="entry")
    _make_camera(db_session, store_id=store_id, camera_id="CAM_J2", role="zone")
    admin_headers = _auth_header(db_session, Role.ADMIN, store_id=store_id, email="admin_j@example.com")
    client.post(
        f"/stores/{store_id}/camera-adjacency",
        json={
            "from_camera_id": "CAM_J1",
            "to_camera_id": "CAM_J2",
            "min_transit_seconds": 60,
            "max_transit_seconds": 180,
        },
        headers=admin_headers,
    )
    # Two entities on CAM_J2 in-window for the same CAM_J1 entity -> ambiguous.
    _make_entity_with_event(db_session, store_id=store_id, entity_id="ENT_J1", camera_id="CAM_J1", ts=BASE_TIME)
    _make_entity_with_event(
        db_session, store_id=store_id, entity_id="ENT_J2", camera_id="CAM_J2", ts=BASE_TIME + timedelta(seconds=100)
    )
    _make_entity_with_event(
        db_session, store_id=store_id, entity_id="ENT_J3", camera_id="CAM_J2", ts=BASE_TIME + timedelta(seconds=140)
    )

    manager_headers = _auth_header(db_session, Role.MANAGER, store_id=store_id, email="manager_j@example.com")
    client.post(f"/stores/{store_id}/identity-link-candidates", headers=manager_headers)

    analyst_headers = _auth_header(db_session, Role.ANALYST, store_id=store_id, email="analyst_j@example.com")
    all_response = client.get(f"/stores/{store_id}/identity-link-candidates", headers=analyst_headers)
    accepted_response = client.get(
        f"/stores/{store_id}/identity-link-candidates", params={"accepted": "true"}, headers=analyst_headers
    )

    assert all_response.status_code == 200
    assert len(all_response.json()) == 2
    assert all(row["reason"] == "ambiguous" for row in all_response.json())

    assert accepted_response.status_code == 200
    assert accepted_response.json() == []


def test_list_candidates_requires_authentication(client: TestClient) -> None:
    response = client.get("/stores/ST_LNK_K/identity-link-candidates")
    assert response.status_code == 401


def test_identity_linking_routes_reject_unknown_store_access(client: TestClient, db_session: Session) -> None:
    headers = _seed_user_without_store_access(db_session, "no_access_link@example.com")

    response = client.get("/stores/ST_LNK_UNKNOWN/identity-link-candidates", headers=headers)

    # require_store_role itself 403s before the service ever runs, since the
    # caller has no StoreAccess row for this store -- same behavior as every
    # other store-scoped route in this project (see
    # test_video_processing_api.py's equivalent test).
    assert response.status_code == 403
