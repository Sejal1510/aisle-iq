# P3.6: route wiring for the new live/analytics endpoints -- both direct
# route-function calls (matching tests/test_analytics_api.py's convention)
# and a couple of real HTTP-layer checks for query-param parsing and the
# ValueError -> 400 conversion (matching tests/test_api_integration.py).
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.stores import (
    get_current_occupancy,
    get_current_queue,
    get_hourly_footfall,
    get_occupancy_history,
    get_peak_hours,
    get_period_comparison,
    get_queue_metrics,
)
from app.db.session import get_db
from app.main import app
from app.models import Base
from app.models.enums import EventType
from app.models.event import Event
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession

BASE_TIME = datetime(2026, 6, 1, 8, 0, 0)


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
def client(test_sessionmaker) -> TestClient:
    def override_get_db():
        db = test_sessionmaker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _seed(db: Session, store_id: str = "ST_LIVE") -> None:
    db.add(Store(id=store_id, name=None))
    db.add(TrackedEntity(id="V1", store_id=store_id, is_staff=False))
    session = VisitSession(tracked_entity_id="V1", store_id=store_id, entry_time=BASE_TIME)
    db.add(session)
    db.flush()
    db.add(
        Event(
            session_id=session.id,
            tracked_entity_id="V1",
            store_id=store_id,
            event_type=EventType.ENTRY,
            timestamp=BASE_TIME,
        )
    )
    db.add(
        Event(
            session_id=session.id,
            tracked_entity_id="V1",
            store_id=store_id,
            event_type=EventType.BILLING_QUEUE_JOIN,
            timestamp=BASE_TIME + timedelta(minutes=5),
            queue_event_id="Q1",
        )
    )
    db.commit()


def test_current_occupancy_route(db_session: Session) -> None:
    _seed(db_session)

    response = get_current_occupancy("ST_LIVE", as_of=BASE_TIME + timedelta(minutes=1), db=db_session)

    assert response.store_id == "ST_LIVE"
    assert response.occupancy == 1


def test_occupancy_history_route(db_session: Session) -> None:
    _seed(db_session)

    response = get_occupancy_history(
        "ST_LIVE", start=BASE_TIME, end=BASE_TIME + timedelta(hours=1), bucket_minutes=30, db=db_session
    )

    assert response.bucket_minutes == 30
    # occupancy_series samples are inclusive of `end` (point-in-time snapshots,
    # not interval sums) -- 08:00, 08:30, 09:00.
    assert len(response.points) == 3


def test_current_queue_route(db_session: Session) -> None:
    _seed(db_session)

    response = get_current_queue("ST_LIVE", as_of=BASE_TIME + timedelta(minutes=10), db=db_session)

    assert response.queue_length == 1
    assert response.queued_entities[0].queue_event_id == "Q1"


def test_queue_metrics_route(db_session: Session) -> None:
    _seed(db_session)

    response = get_queue_metrics(
        "ST_LIVE", start=BASE_TIME, end=BASE_TIME + timedelta(hours=1), db=db_session
    )

    assert response.completed_visits == 0
    assert response.abandoned_visits == 0


def test_hourly_footfall_route(db_session: Session) -> None:
    _seed(db_session)

    response = get_hourly_footfall(
        "ST_LIVE", start=BASE_TIME, end=BASE_TIME + timedelta(hours=1), bucket_minutes=60, db=db_session
    )

    assert response.buckets[0].entries == 1


def test_peak_hours_route(db_session: Session) -> None:
    _seed(db_session)

    response = get_peak_hours(
        "ST_LIVE", start=BASE_TIME, end=BASE_TIME + timedelta(hours=1), bucket_minutes=60, db=db_session
    )

    assert response.peak_hour.entries == 1


def test_period_comparison_route(db_session: Session) -> None:
    _seed(db_session)

    response = get_period_comparison(
        "ST_LIVE", start=BASE_TIME, end=BASE_TIME + timedelta(hours=1), db=db_session
    )

    assert response.current.footfall == 1
    assert response.previous.footfall == 0


def test_occupancy_history_via_http_returns_200_with_query_params(client: TestClient, db_session: Session) -> None:
    _seed(db_session)

    response = client.get(
        "/stores/ST_LIVE/occupancy/history",
        params={"start": BASE_TIME.isoformat(), "end": (BASE_TIME + timedelta(hours=1)).isoformat(), "bucket_minutes": 30},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["store_id"] == "ST_LIVE"
    assert len(body["points"]) == 3


def test_invalid_range_returns_400_via_http(client: TestClient, db_session: Session) -> None:
    _seed(db_session)

    response = client.get(
        "/stores/ST_LIVE/queue/metrics",
        params={"start": BASE_TIME.isoformat(), "end": BASE_TIME.isoformat()},
    )

    assert response.status_code == 400
    assert "end must be after start" in response.json()["detail"]


def test_missing_required_range_returns_422_via_http(client: TestClient, db_session: Session) -> None:
    _seed(db_session)

    response = client.get("/stores/ST_LIVE/footfall/hourly")

    assert response.status_code == 422
