# P8: HTTP-layer coverage for the new spatial-config/spatial-intensity routes
# -- real ASGI requests (RBAC dependency wiring, query-param validation,
# response serialization), mirroring tests/test_api_integration.py's fixture
# pattern. Service-level correctness is tests/test_spatial_intelligence_service.py's
# job; this file is about the routes themselves.
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
from app.models.enums import Role, ZoneType
from app.models.store import Store
from app.services.store_config_service import PolygonGeometry, StoreConfigService

STORE_ID = "ST_SPATIAL_HTTP"


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
def seed_store_with_zone(db_session: Session):
    db_session.add(Store(id=STORE_ID, name=None))
    db_session.flush()
    StoreConfigService(db_session).create_zone(
        STORE_ID,
        "ZONE_A",
        name="Zone A",
        zone_type=ZoneType.SHELF,
        map_polygon=PolygonGeometry(points=((0.1, 0.1), (0.5, 0.1), (0.5, 0.5))),
    )
    db_session.commit()


def _analyst_headers(db_session: Session, store_id: str = STORE_ID, email: str = "analyst@example.com") -> dict:
    user = create_user(db_session, email=email, raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, user.id, store_id, Role.ANALYST)
    db_session.commit()
    token, _ = create_access_token(user.id)
    return {"Authorization": f"Bearer {token}"}


def test_spatial_config_requires_authentication(client: TestClient) -> None:
    response = client.get(f"/stores/{STORE_ID}/spatial-config")
    assert response.status_code == 401


def test_spatial_config_requires_store_access(client: TestClient, db_session: Session, seed_store_with_zone) -> None:
    other_store_headers = _analyst_headers(db_session, store_id="ST_SPATIAL_OTHER", email="other@example.com")
    if db_session.get(Store, "ST_SPATIAL_OTHER") is None:
        db_session.add(Store(id="ST_SPATIAL_OTHER", name=None))
        db_session.commit()

    response = client.get(f"/stores/{STORE_ID}/spatial-config", headers=other_store_headers)
    assert response.status_code == 403


def test_spatial_config_returns_zone_with_polygon(client: TestClient, db_session: Session, seed_store_with_zone) -> None:
    headers = _analyst_headers(db_session)

    response = client.get(f"/stores/{STORE_ID}/spatial-config", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["store_id"] == STORE_ID
    assert len(body["zones"]) == 1
    assert body["zones"][0]["zone_id"] == "ZONE_A"
    assert body["zones"][0]["map_polygon"] == [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]]


def test_spatial_intensity_defaults_to_visits_metric(client: TestClient, db_session: Session, seed_store_with_zone) -> None:
    headers = _analyst_headers(db_session)

    response = client.get(f"/stores/{STORE_ID}/spatial-intensity", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["metric"] == "visits"
    assert body["zones"][0]["zone_id"] == "ZONE_A"
    assert body["zones"][0]["visits"] == 0  # zone configured, zero activity -- still present


def test_spatial_intensity_accepts_dwell_metric(client: TestClient, db_session: Session, seed_store_with_zone) -> None:
    headers = _analyst_headers(db_session)

    response = client.get(f"/stores/{STORE_ID}/spatial-intensity?metric=dwell", headers=headers)

    assert response.status_code == 200
    assert response.json()["metric"] == "dwell"


def test_spatial_intensity_rejects_unsupported_metric(client: TestClient, db_session: Session, seed_store_with_zone) -> None:
    headers = _analyst_headers(db_session)

    response = client.get(f"/stores/{STORE_ID}/spatial-intensity?metric=revenue", headers=headers)

    # FastAPI validates the Literal["visits", "dwell"] query param before the
    # route body (and SpatialIntelligenceService's own ValueError check) ever run.
    assert response.status_code == 422


def test_spatial_config_for_store_with_no_zones_returns_empty_list(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id=STORE_ID, name=None))
    db_session.commit()
    headers = _analyst_headers(db_session)

    response = client.get(f"/stores/{STORE_ID}/spatial-config", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["zones"] == []
    assert body["layout_image_url"] is None
