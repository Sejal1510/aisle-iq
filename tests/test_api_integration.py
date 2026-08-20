# PROMPT: Regression coverage for F-05/F-06/F-07 -- real HTTP-layer tests through
# the actual ASGI application (request parsing, dependency wiring, response
# serialization), not direct Python calls into route functions. These are
# additive to the existing direct-call service/route tests, not a replacement.
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.core.security import create_api_key
from app.db.session import get_db
from app.main import app
from app.models import Base
from app.models.store import Store
from app.services.event_ingestion_service import EventIngestionService


@pytest.fixture()
def test_sessionmaker():
    # StaticPool + check_same_thread=False: FastAPI runs sync `def` routes in a
    # worker thread via TestClient, and plain in-memory SQLite ties its database
    # to the connecting thread by default -- StaticPool shares one connection
    # across threads so the route handler sees the same in-memory database the
    # test fixtures set up.
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
    # /health uses SessionLocal directly rather than the get_db dependency;
    # patch it too so the whole app is isolated to this test's database.
    monkeypatch.setattr(main_module, "SessionLocal", test_sessionmaker)

    try:
        # Deliberately not entered as a context manager: doing so would run the
        # app's lifespan, which calls init_db() against the real configured
        # database rather than this test's isolated in-memory one.
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def store_api_key(db_session: Session) -> str:
    db_session.add(Store(id="ST_HTTP", name=None))
    db_session.flush()
    _row, raw_key = create_api_key(db_session, "ST_HTTP")
    db_session.commit()
    return raw_key


def _entry_payload(event_id: str = "evt-http-1") -> dict:
    return {
        "event_id": event_id,
        "event_type": "entry",
        "id_token": "ID_HTTP_1",
        "store_code": "ST_HTTP",
        "camera_id": "CAM_ENTRY_1",
        "event_timestamp": "2026-06-01T09:00:00",
    }


def test_events_endpoint_via_real_http_request(client: TestClient, store_api_key: str) -> None:
    response = client.post(
        "/events/",
        json=_entry_payload(),
        headers={"X-API-Key": store_api_key},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["event_type"] == "entry"
    assert body["tracked_entity_id"]


def test_batch_event_ingestion_via_http(client: TestClient, store_api_key: str) -> None:
    response = client.post(
        "/events/ingest",
        json=[_entry_payload("evt-http-batch-1"), _entry_payload("evt-http-batch-2")],
        headers={"X-API-Key": store_api_key},
    )

    assert response.status_code == 207
    body = response.json()
    assert body["accepted"] == 2
    assert body["failed"] == 0
    assert len(body["results"]) == 2


def test_ingestion_requires_valid_api_key(client: TestClient, store_api_key: str) -> None:
    missing_key_response = client.post("/events/", json=_entry_payload("evt-http-noauth"))
    assert missing_key_response.status_code == 401

    invalid_key_response = client.post(
        "/events/",
        json=_entry_payload("evt-http-badauth"),
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert invalid_key_response.status_code == 401

    valid_key_response = client.post(
        "/events/",
        json=_entry_payload("evt-http-goodauth"),
        headers={"X-API-Key": store_api_key},
    )
    assert valid_key_response.status_code == 202


def test_single_event_rejects_cross_store_submission(client: TestClient, store_api_key: str) -> None:
    """P2.7: a valid key authenticates the caller but only authorizes them for
    their own store -- submitting an event for a different store must be
    rejected, not silently accepted."""
    payload = _entry_payload("evt-http-wrong-store")
    payload["store_code"] = "ST_SOMEONE_ELSE"

    response = client.post("/events/", json=payload, headers={"X-API-Key": store_api_key})

    assert response.status_code == 403
    assert response.json()["error"] == "store_not_authorized"


def test_batch_ingestion_rejects_cross_store_items_but_keeps_valid_ones(
    client: TestClient, store_api_key: str
) -> None:
    """A mixed batch must reject only the items for an unauthorized store,
    not the whole batch -- consistent with the batch route's existing
    per-item partial-failure design."""
    own_store_event = _entry_payload("evt-http-batch-own-store")
    other_store_event = _entry_payload("evt-http-batch-other-store")
    other_store_event["store_code"] = "ST_SOMEONE_ELSE"

    response = client.post(
        "/events/ingest",
        json=[own_store_event, other_store_event],
        headers={"X-API-Key": store_api_key},
    )

    assert response.status_code == 207
    body = response.json()
    assert body["accepted"] == 1
    assert body["failed"] == 1
    assert body["results"][0]["status"] == "accepted"
    assert body["results"][1]["status"] == "failed"
    assert "not authorized" in body["results"][1]["error"]


def test_batch_ingestion_requires_valid_api_key(client: TestClient) -> None:
    response = client.post("/events/ingest", json=[_entry_payload("evt-http-batch-noauth")])
    assert response.status_code == 401


def test_single_event_validation_returns_422(client: TestClient, store_api_key: str) -> None:
    malformed_payload = {"event_type": "not_a_real_event_type"}

    response = client.post(
        "/events/",
        json=malformed_payload,
        headers={"X-API-Key": store_api_key},
    )

    assert response.status_code == 422


def test_single_event_unexpected_error_returns_500_without_leaking_internals(
    client: TestClient,
    store_api_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_process_event(self, payload, **_kwargs):
        raise RuntimeError("simulated unexpected failure with internal detail")

    monkeypatch.setattr(EventIngestionService, "process_event", broken_process_event)

    response = client.post(
        "/events/",
        json=_entry_payload("evt-http-crash"),
        headers={"X-API-Key": store_api_key},
    )

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "ingestion_failed"
    assert "simulated unexpected failure" not in body["message"]
    assert "RuntimeError" not in str(body)


def test_metrics_endpoint_via_http(client: TestClient, store_api_key: str) -> None:
    client.post("/events/", json=_entry_payload("evt-http-metrics"), headers={"X-API-Key": store_api_key})

    response = client.get("/stores/ST_HTTP/metrics")

    assert response.status_code == 200
    body = response.json()
    assert body["store_id"] == "ST_HTTP"
    assert "total_visitors" in body
    assert "conversion_rate" in body


def test_funnel_endpoint_via_http(client: TestClient) -> None:
    response = client.get("/stores/ST_HTTP/funnel")

    assert response.status_code == 200
    body = response.json()
    assert body["store_id"] == "ST_HTTP"
    assert [step["step"] for step in body["steps"]] == ["visitors", "queue_join", "queue_complete", "purchase"]


def test_health_endpoint_via_http(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
