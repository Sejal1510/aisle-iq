# P6: PostgreSQL end-to-end smoke coverage. Not a duplicate of the SQLite
# suite (see docs/CHOICES.md for why the rest of the suite deliberately stays
# on fast, dependency-free SQLite) -- this exercises a handful of
# representative flows against a real PostgreSQL connection to catch
# anything dialect-specific the SQLite-only suite structurally cannot:
# event ingestion through the real HTTP layer, direct ORM writes feeding
# CorrelationService's joins/filters, a role-gated analytics route, and
# whether the UTC-default fix in app/db/base.py actually holds against a
# live PostgreSQL server (not just SQLite's always-UTC CURRENT_TIMESTAMP).
#
# Opt-in via AISLEIQ_TEST_POSTGRES_URL -- skipped entirely otherwise, same as
# tests/test_postgres_migration.py.
import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

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
from app.models.enums import CorrelationStatus, EventType, Role
from app.models.event import Event
from app.models.pos import PosTransaction, PosTransactionItem
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession
from app.services.correlation_service import CorrelationService

POSTGRES_URL = os.environ.get("AISLEIQ_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="AISLEIQ_TEST_POSTGRES_URL not set -- PostgreSQL verification is opt-in",
)


@pytest.fixture()
def test_sessionmaker():
    engine = create_engine(POSTGRES_URL)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        # drop_all (unlike Alembic's op.drop_table -- see
        # tests/test_postgres_migration.py) does also drop the native enum
        # TYPEs it created, so this leaves the database empty for the next run.
        Base.metadata.drop_all(bind=engine)
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
        # Not entered as a context manager: that would run the app lifespan,
        # which calls init_db() against the real configured database rather
        # than this test's isolated PostgreSQL setup.
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_event_ingestion_over_http_against_postgres(db_session: Session, client: TestClient) -> None:
    db_session.add(Store(id="ST_PG_HTTP", name=None))
    db_session.flush()
    _row, raw_key = create_api_key(db_session, "ST_PG_HTTP")
    db_session.commit()

    response = client.post(
        "/events/",
        json={
            "event_id": "evt-pg-1",
            "event_type": "entry",
            "id_token": "ID_PG_1",
            "store_code": "ST_PG_HTTP",
            "camera_id": "CAM_ENTRY_1",
            "event_timestamp": "2026-06-01T09:00:00",
        },
        headers={"X-API-Key": raw_key},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["tracked_entity_id"]


def test_created_at_default_is_utc_on_postgres(db_session: Session) -> None:
    """Regression coverage for the func.now() -> Python-side utcnow() fix in
    app/db/base.py: created_at must reflect real UTC time when the row lands
    in PostgreSQL, not the database session's configured timezone."""
    store = Store(id="ST_PG_UTC", name=None)
    db_session.add(store)
    db_session.commit()
    db_session.refresh(store)

    assert abs((datetime.now(UTC).replace(tzinfo=None) - store.created_at).total_seconds()) < 10


def test_correlation_service_matches_across_postgres_joins(db_session: Session) -> None:
    """Exercises CorrelationService's real query surface (joins, .in_(),
    .is_not(), ordering, delete+insert) against PostgreSQL rather than
    SQLite -- this is exactly the kind of cross-table, filtered query code
    that a dialect quirk could silently misbehave on. Includes a queue
    completion event (not just exit_time) so the resulting confidence score
    clears the matching threshold with a comfortable margin, rather than
    sitting at the theoretical maximum for an exit_time-only match."""
    store = Store(id="ST_PG_CORR", name=None)
    entity = TrackedEntity(store_id="ST_PG_CORR", is_staff=False)
    db_session.add_all([store, entity])
    db_session.flush()

    entry_time = datetime(2026, 6, 1, 9, 0, 0)
    queue_completed_time = datetime(2026, 6, 1, 9, 29, 0)
    exit_time = datetime(2026, 6, 1, 9, 30, 0)
    session = VisitSession(
        tracked_entity_id=entity.id,
        store_id="ST_PG_CORR",
        entry_time=entry_time,
        exit_time=exit_time,
    )
    db_session.add(session)
    db_session.flush()

    queue_event = Event(
        session_id=session.id,
        tracked_entity_id=entity.id,
        store_id="ST_PG_CORR",
        event_type=EventType.QUEUE_COMPLETED,
        timestamp=queue_completed_time,
        abandoned=False,
    )
    db_session.add(queue_event)

    transaction = PosTransaction(
        order_id="ORD-PG-1",
        store_id="ST_PG_CORR",
        timestamp=exit_time + timedelta(seconds=30),
    )
    transaction.items = [PosTransactionItem(product_id="P1", amount=100.0)]
    db_session.add(transaction)
    db_session.commit()

    correlations = CorrelationService(db_session).correlate_all()
    db_session.commit()

    matched = [c for c in correlations if c.status == CorrelationStatus.MATCHED]
    assert len(matched) == 1
    assert matched[0].transaction_id == transaction.id
    assert matched[0].session_id == session.id
    assert matched[0].confidence_score >= 0.55


def test_role_gated_analytics_route_against_postgres(db_session: Session, client: TestClient) -> None:
    db_session.add(Store(id="ST_PG_AUTH", name=None))
    user = create_user(db_session, email="pg-analyst@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, user.id, "ST_PG_AUTH", Role.ANALYST)
    db_session.commit()
    token, _ = create_access_token(user.id)

    response = client.get(
        "/stores/ST_PG_AUTH/metrics",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200


def test_replay_workflow_over_http_against_postgres(db_session: Session, client: TestClient) -> None:
    """Exercises the replay feature's own dialect-sensitive surface against a
    real PostgreSQL connection: RawEvent.store_id (a nullable column added to
    an existing table) and the native replaysourcetype/replaystatus enum
    columns on the new replay_job table."""
    db_session.add(Store(id="ST_PG_REPLAY", name=None))
    db_session.flush()
    _row, raw_key = create_api_key(db_session, "ST_PG_REPLAY")
    user = create_user(db_session, email="pg-replay-manager@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, user.id, "ST_PG_REPLAY", Role.MANAGER)
    db_session.commit()
    token, _ = create_access_token(user.id)

    ingest_response = client.post(
        "/events/",
        json={
            "event_id": "evt-pg-replay-1",
            "event_type": "entry",
            "id_token": "ID_PG_REPLAY_1",
            "store_code": "ST_PG_REPLAY",
            "camera_id": "CAM_ENTRY_1",
            "event_timestamp": "2026-06-01T09:00:00",
        },
        headers={"X-API-Key": raw_key},
    )
    assert ingest_response.status_code == 202

    replay_response = client.post(
        "/stores/ST_PG_REPLAY/replay",
        json={"source_type": "raw_event_archive"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert replay_response.status_code == 201
    body = replay_response.json()
    assert body["status"] == "completed"
    assert body["total_events"] == 1
    assert body["duplicate_events"] == 1
    assert body["accepted_events"] == 0

    status_response = client.get(
        f"/stores/ST_PG_REPLAY/replay/{body['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "completed"
