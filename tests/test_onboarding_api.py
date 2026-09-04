# P7: HTTP-layer tests for the store onboarding/configuration API
# (app/api/onboarding.py) -- store creation, map upload, zone/camera/
# coverage CRUD, RBAC gating, and (P7 security hardening) traversal-style
# identifier rejection, upload size limits, and file-type allowlisting,
# mirroring tests/test_auth_api.py's TestClient conventions. Unit-level
# tests for the underlying app/core/storage.py defenses live in
# tests/test_storage_security.py.
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.onboarding as onboarding_module
import app.main as main_module
from app.core.security import create_access_token, create_user, grant_store_access
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
def logged_in_user(db_session: Session) -> dict:
    user = create_user(db_session, email="onboarder@aisleiq.local", raw_password="pw-onboard")
    db_session.commit()
    token, _ = create_access_token(user.id)
    return {"user_id": user.id, "token": token}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_create_store_requires_authentication(client: TestClient) -> None:
    response = client.post("/stores", json={"store_id": "ST_NEW", "name": "New Store"})
    assert response.status_code == 401


def test_create_store_grants_creator_admin_access(client: TestClient, logged_in_user: dict) -> None:
    response = client.post(
        "/stores", json={"store_id": "ST_NEW", "name": "New Store"}, headers=_auth(logged_in_user["token"])
    )
    assert response.status_code == 201
    assert response.json() == {"store_id": "ST_NEW", "name": "New Store"}

    # The creator can now write config for the store they just created --
    # proof that store creation itself granted ADMIN, not just a bare row.
    zone_response = client.post(
        "/stores/ST_NEW/config/zones",
        json={"zone_id": "Z1", "name": "Entrance", "zone_type": "ENTRY_DOOR"},
        headers=_auth(logged_in_user["token"]),
    )
    assert zone_response.status_code == 201


def test_create_store_conflicts_on_duplicate_id(client: TestClient, logged_in_user: dict) -> None:
    first = client.post("/stores", json={"store_id": "ST_DUP"}, headers=_auth(logged_in_user["token"]))
    assert first.status_code == 201
    second = client.post("/stores", json={"store_id": "ST_DUP"}, headers=_auth(logged_in_user["token"]))
    assert second.status_code == 409


def test_zone_write_requires_admin_not_just_any_grant(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id="ST_RBAC"))
    analyst = create_user(db_session, email="analyst-only@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, analyst.id, "ST_RBAC", Role.ANALYST)
    db_session.commit()
    token, _ = create_access_token(analyst.id)

    response = client.post(
        "/stores/ST_RBAC/config/zones",
        json={"zone_id": "Z1", "name": "Entrance", "zone_type": "ENTRY_DOOR"},
        headers=_auth(token),
    )
    assert response.status_code == 403


def test_zone_read_allows_analyst(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id="ST_RBAC2"))
    analyst = create_user(db_session, email="analyst2@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, analyst.id, "ST_RBAC2", Role.ANALYST)
    db_session.commit()
    token, _ = create_access_token(analyst.id)

    response = client.get("/stores/ST_RBAC2/config/zones", headers=_auth(token))
    assert response.status_code == 200
    assert response.json() == []


def test_zone_crud_round_trip(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id="ST_ZONE"))
    admin = create_user(db_session, email="zone-admin@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, admin.id, "ST_ZONE", Role.ADMIN)
    db_session.commit()
    token, _ = create_access_token(admin.id)

    create_response = client.post(
        "/stores/ST_ZONE/config/zones",
        json={
            "zone_id": "Z_MOBILES",
            "name": "Mobiles",
            "zone_type": "SHELF",
            "is_revenue_zone": True,
            "map_polygon": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4], [0.1, 0.4]],
        },
        headers=_auth(token),
    )
    assert create_response.status_code == 201
    body = create_response.json()
    assert body["zone_id"] == "Z_MOBILES"
    assert body["map_polygon"] == [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4], [0.1, 0.4]]

    update_response = client.patch(
        "/stores/ST_ZONE/config/zones/Z_MOBILES",
        json={"name": "Mobiles & Accessories"},
        headers=_auth(token),
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "Mobiles & Accessories"

    list_response = client.get("/stores/ST_ZONE/config/zones", headers=_auth(token))
    assert [z["zone_id"] for z in list_response.json()] == ["Z_MOBILES"]


def test_camera_and_coverage_round_trip(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id="ST_CAM"))
    admin = create_user(db_session, email="cam-admin@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, admin.id, "ST_CAM", Role.ADMIN)
    db_session.commit()
    token, _ = create_access_token(admin.id)
    headers = _auth(token)

    zone_response = client.post(
        "/stores/ST_CAM/config/zones",
        json={"zone_id": "Z1", "name": "Laptops", "zone_type": "SHELF", "is_revenue_zone": True},
        headers=headers,
    )
    assert zone_response.status_code == 201

    camera_response = client.post(
        "/stores/ST_CAM/config/cameras",
        json={"camera_id": "CAM1", "name": "Laptop Zone Cam", "role": "zone", "video_path": "videos/cam1.mp4"},
        headers=headers,
    )
    assert camera_response.status_code == 201
    assert camera_response.json()["role"] == "zone"

    coverage_response = client.post(
        "/stores/ST_CAM/config/cameras/CAM1/coverage",
        json={
            "zone_id": "Z1",
            "geometry": {"kind": "polygon", "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]},
        },
        headers=headers,
    )
    assert coverage_response.status_code == 201
    coverage_body = coverage_response.json()
    assert coverage_body["zone_id"] == "Z1"
    assert coverage_body["geometry"]["kind"] == "polygon"

    list_response = client.get("/stores/ST_CAM/config/cameras/CAM1/coverage", headers=headers)
    assert len(list_response.json()) == 1

    delete_response = client.delete(
        f"/stores/ST_CAM/config/cameras/CAM1/coverage/{coverage_body['coverage_id']}", headers=headers
    )
    assert delete_response.status_code == 204
    assert client.get("/stores/ST_CAM/config/cameras/CAM1/coverage", headers=headers).json() == []


def test_entry_line_coverage_round_trip(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id="ST_LINE"))
    admin = create_user(db_session, email="line-admin@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, admin.id, "ST_LINE", Role.ADMIN)
    db_session.commit()
    headers = _auth(create_access_token(admin.id)[0])

    client.post(
        "/stores/ST_LINE/config/cameras",
        json={"camera_id": "CAM_ENTRY", "role": "entry", "video_path": "videos/entry.mp4"},
        headers=headers,
    )
    response = client.post(
        "/stores/ST_LINE/config/cameras/CAM_ENTRY/coverage",
        json={"geometry": {"kind": "line", "axis": "x", "position": 0.5, "inside_greater_than_position": True}},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["zone_id"] is None
    assert response.json()["geometry"] == {
        "kind": "line",
        "points": None,
        "axis": "x",
        "position": 0.5,
        "inside_greater_than_position": True,
    }


def test_map_upload_and_retrieval(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id="ST_MAP"))
    admin = create_user(db_session, email="map-admin@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, admin.id, "ST_MAP", Role.ADMIN)
    db_session.commit()
    headers = _auth(create_access_token(admin.id)[0])

    fake_png = io.BytesIO(b"\x89PNG\r\n\x1a\nfake-map-bytes")
    upload_response = client.post(
        "/stores/ST_MAP/config/maps",
        files={"file": ("floorplan.png", fake_png, "image/png")},
        data={"name": "Ground Floor"},
        headers=headers,
    )
    assert upload_response.status_code == 201
    map_body = upload_response.json()
    assert map_body["name"] == "Ground Floor"
    assert map_body["is_active"] is True
    assert map_body["file_url"] == f"/stores/ST_MAP/config/maps/{map_body['map_id']}/file"

    file_response = client.get(map_body["file_url"], headers=headers)
    assert file_response.status_code == 200
    assert file_response.content == b"\x89PNG\r\n\x1a\nfake-map-bytes"

    list_response = client.get("/stores/ST_MAP/config/maps", headers=headers)
    assert len(list_response.json()) == 1


def test_camera_update_is_scoped_to_store(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id="ST_A"))
    db_session.add(Store(id="ST_B"))
    admin_a = create_user(db_session, email="admin-a@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, admin_a.id, "ST_A", Role.ADMIN)
    grant_store_access(db_session, admin_a.id, "ST_B", Role.ADMIN)
    db_session.commit()
    headers = _auth(create_access_token(admin_a.id)[0])

    client.post(
        "/stores/ST_A/config/cameras",
        json={"camera_id": "CAM_A", "role": "zone", "video_path": "a.mp4"},
        headers=headers,
    )
    response = client.patch(
        "/stores/ST_B/config/cameras/CAM_A",
        json={"name": "Hijacked"},
        headers=headers,
    )
    assert response.status_code == 404


# ----------------------------------------------------------------------
# P7 security hardening: traversal-style identifiers, upload size limits,
# and upload file-type allowlisting.
# ----------------------------------------------------------------------
@pytest.mark.parametrize("bad_store_id", ["../../evil", "..\\evil", "a/b", "a\\b", "..", ""])
def test_create_store_rejects_traversal_style_id(client: TestClient, logged_in_user: dict, bad_store_id: str) -> None:
    response = client.post(
        "/stores", json={"store_id": bad_store_id, "name": "x"}, headers=_auth(logged_in_user["token"])
    )
    assert response.status_code == 422


@pytest.mark.parametrize("valid_store_id", ["ST1001", "ST1002", "ST_ELECTRONICS_9001"])
def test_create_store_accepts_legacy_and_new_style_ids(
    client: TestClient, logged_in_user: dict, valid_store_id: str
) -> None:
    response = client.post(
        "/stores", json={"store_id": valid_store_id}, headers=_auth(logged_in_user["token"])
    )
    assert response.status_code == 201
    assert response.json()["store_id"] == valid_store_id


def test_create_zone_rejects_traversal_style_id(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id="ST_ZONE_SEC"))
    admin = create_user(db_session, email="zone-sec-admin@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, admin.id, "ST_ZONE_SEC", Role.ADMIN)
    db_session.commit()
    headers = _auth(create_access_token(admin.id)[0])

    response = client.post(
        "/stores/ST_ZONE_SEC/config/zones",
        json={"zone_id": "../../escape", "name": "Evil", "zone_type": "OTHER"},
        headers=headers,
    )
    assert response.status_code == 422


def test_create_camera_rejects_traversal_style_id(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id="ST_CAM_SEC"))
    admin = create_user(db_session, email="cam-sec-admin@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, admin.id, "ST_CAM_SEC", Role.ADMIN)
    db_session.commit()
    headers = _auth(create_access_token(admin.id)[0])

    response = client.post(
        "/stores/ST_CAM_SEC/config/cameras",
        json={"camera_id": "..\\..\\escape", "role": "zone"},
        headers=headers,
    )
    assert response.status_code == 422


def _admin_headers_for_new_store(db_session: Session, store_id: str, email: str) -> dict:
    db_session.add(Store(id=store_id))
    admin = create_user(db_session, email=email, raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, admin.id, store_id, Role.ADMIN)
    db_session.commit()
    return _auth(create_access_token(admin.id)[0])


def test_map_upload_rejects_oversized_file(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = _admin_headers_for_new_store(db_session, "ST_MAP_BIG", "big-map-admin@example.com")
    monkeypatch.setattr(
        onboarding_module,
        "get_settings",
        lambda: SimpleNamespace(max_map_upload_bytes=10, max_camera_reference_upload_bytes=10),
    )

    oversized = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * 1000)
    response = client.post(
        "/stores/ST_MAP_BIG/config/maps",
        files={"file": ("big.png", oversized, "image/png")},
        headers=headers,
    )
    assert response.status_code == 413


def test_camera_reference_upload_rejects_oversized_file(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _admin_headers_for_new_store(db_session, "ST_REF_BIG", "big-ref-admin@example.com")
    client.post(
        "/stores/ST_REF_BIG/config/cameras",
        json={"camera_id": "CAM_BIG", "role": "zone", "video_path": "a.mp4"},
        headers=headers,
    )
    monkeypatch.setattr(
        onboarding_module,
        "get_settings",
        lambda: SimpleNamespace(max_map_upload_bytes=10, max_camera_reference_upload_bytes=10),
    )

    oversized = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * 1000)
    response = client.post(
        "/stores/ST_REF_BIG/config/cameras/CAM_BIG/reference-image",
        files={"file": ("big.png", oversized, "image/png")},
        headers=headers,
    )
    assert response.status_code == 413


def test_map_upload_rejects_unsupported_file_type(client: TestClient, db_session: Session) -> None:
    headers = _admin_headers_for_new_store(db_session, "ST_MAP_BADTYPE", "badtype-map-admin@example.com")

    response = client.post(
        "/stores/ST_MAP_BADTYPE/config/maps",
        files={"file": ("payload.exe", io.BytesIO(b"MZ..."), "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 400


def test_camera_reference_upload_rejects_unsupported_file_type(client: TestClient, db_session: Session) -> None:
    headers = _admin_headers_for_new_store(db_session, "ST_REF_BADTYPE", "badtype-ref-admin@example.com")
    client.post(
        "/stores/ST_REF_BADTYPE/config/cameras",
        json={"camera_id": "CAM_BADTYPE", "role": "zone", "video_path": "a.mp4"},
        headers=headers,
    )

    response = client.post(
        "/stores/ST_REF_BADTYPE/config/cameras/CAM_BADTYPE/reference-image",
        files={"file": ("frame.svg", io.BytesIO(b"<svg><script>1</script></svg>"), "image/svg+xml")},
        headers=headers,
    )
    assert response.status_code == 400


def test_camera_reference_upload_accepts_valid_image(client: TestClient, db_session: Session) -> None:
    headers = _admin_headers_for_new_store(db_session, "ST_REF_OK", "ok-ref-admin@example.com")
    client.post(
        "/stores/ST_REF_OK/config/cameras",
        json={"camera_id": "CAM_OK", "role": "zone", "video_path": "a.mp4"},
        headers=headers,
    )

    upload_response = client.post(
        "/stores/ST_REF_OK/config/cameras/CAM_OK/reference-image",
        files={"file": ("frame.png", io.BytesIO(b"\x89PNG\r\n\x1a\nreference-bytes"), "image/png")},
        headers=headers,
    )
    assert upload_response.status_code == 200
    body = upload_response.json()
    assert body["reference_image_url"] == "/stores/ST_REF_OK/config/cameras/CAM_OK/reference-image"

    file_response = client.get(body["reference_image_url"], headers=headers)
    assert file_response.status_code == 200
    assert file_response.content == b"\x89PNG\r\n\x1a\nreference-bytes"
