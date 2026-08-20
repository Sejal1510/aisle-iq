# PROMPT: Generate API tests for store analytics endpoints, route registration, and operational anomaly responses.
# CHANGES MADE: Kept direct route-function tests, seeded deterministic analytics data, and added anomaly/route assertions.
import pytest
from sqlalchemy.exc import OperationalError
import asyncio
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.stores import get_store_anomalies, get_store_funnel, get_store_insights, get_store_metrics, get_store_paths
from app.main import app, health_check, structured_request_logging
import app.main as main_module
from app.models import Base
from tests.test_analytics_service import seed_analytics_data


@pytest.fixture()
def api_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        seed_analytics_data(session)
        session.commit()
        yield session
    finally:
        session.close()
        engine.dispose()


def test_metrics_endpoint_returns_store_analytics(api_session: Session) -> None:
    response = get_store_metrics("ST1008", db=api_session)

    assert response.store_id == "ST1008"
    assert response.total_visitors == 3
    assert response.unique_visitors == 3
    assert response.staff_visitors == 1
    assert response.solo_visitors == 1
    assert response.groups == 1
    assert response.average_group_size == 2.0
    assert response.conversion_rate == 0.3333
    assert response.attributed_revenue == 150.0
    assert response.zone_dwell_metrics[0].zone_id == "makeup"


def test_funnel_endpoint_returns_conversion_funnel(api_session: Session) -> None:
    response = get_store_funnel("ST1008", db=api_session)

    assert response.store_id == "ST1008"
    assert response.visitors == 3
    assert response.queue_join == 2
    assert response.queue_complete == 1
    assert response.purchase == 1
    assert [step.step for step in response.steps] == ["visitors", "queue_join", "queue_complete", "purchase"]


def test_insights_endpoint_returns_recommendations(api_session: Session) -> None:
    response = get_store_insights("ST1008", db=api_session)

    assert response.store_id == "ST1008"
    assert any(insight.title == "Queue Abandonment Risk" for insight in response.insights)
    assert all(insight.recommendation for insight in response.insights)


def test_paths_endpoint_returns_customer_journeys(api_session: Session) -> None:
    response = get_store_paths("ST1008", db=api_session)

    assert response.store_id == "ST1008"
    assert response.most_common_paths
    assert response.top_purchase_journeys


def test_anomalies_endpoint_returns_operational_contract(api_session: Session) -> None:
    response = get_store_anomalies("ST1008", db=api_session)

    assert response.store_id == "ST1008"
    assert any(anomaly.anomaly_type == "queue_spike" for anomaly in response.anomalies)
    assert all(anomaly.severity in {"INFO", "WARN", "CRITICAL"} for anomaly in response.anomalies)
    assert all(anomaly.suggested_action for anomaly in response.anomalies)


# ---------------------------------------------------------------------------
# P4.3: anomalies endpoint gains optional, additive start/end trend params.
# ---------------------------------------------------------------------------


def test_anomalies_endpoint_without_range_has_no_trend_fields(api_session: Session) -> None:
    """The exact same call as test_anomalies_endpoint_returns_operational_contract
    above (no start/end) -- explicit proof that the P4.3 schema additions
    never populate when the caller doesn't opt into range-aware behavior."""
    response = get_store_anomalies("ST1008", db=api_session)

    for anomaly in response.anomalies:
        assert anomaly.metric is None
        assert anomaly.related_signals == []


def test_anomalies_endpoint_rejects_start_without_end(api_session: Session) -> None:
    from datetime import datetime

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        get_store_anomalies("ST1008", start=datetime(2026, 4, 10), db=api_session)

    assert exc_info.value.status_code == 400


def test_anomalies_endpoint_rejects_end_without_start(api_session: Session) -> None:
    from datetime import datetime

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        get_store_anomalies("ST1008", end=datetime(2026, 4, 10), db=api_session)

    assert exc_info.value.status_code == 400


def test_anomalies_endpoint_rejects_end_before_start(api_session: Session) -> None:
    from datetime import datetime

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        get_store_anomalies(
            "ST1008", start=datetime(2026, 4, 10), end=datetime(2026, 4, 9), db=api_session
        )

    assert exc_info.value.status_code == 400


def test_anomalies_endpoint_with_valid_range_returns_200_and_may_include_trend_anomalies(
    api_session: Session,
) -> None:
    from datetime import datetime

    response = get_store_anomalies(
        "ST1008",
        start=datetime(2026, 4, 10, 0, 0, 0),
        end=datetime(2026, 4, 11, 0, 0, 0),
        db=api_session,
    )

    assert response.store_id == "ST1008"
    # Static rules still run alongside the (possibly empty) trend rules.
    assert any(anomaly.anomaly_type == "queue_spike" for anomaly in response.anomalies)
    assert all(anomaly.severity in {"INFO", "WARN", "CRITICAL"} for anomaly in response.anomalies)


def test_store_analytics_routes_are_registered() -> None:
    # Read registered paths from the app's own OpenAPI contract rather than
    # walking app.routes directly: Starlette's internal representation of
    # included routers is not a stable API (a Starlette upgrade during this
    # phase changed it to a wrapper object with no `.path` attribute), while
    # the OpenAPI schema is the public, documented surface this test actually
    # cares about.
    route_paths = set(app.openapi()["paths"].keys())

    assert "/stores/{store_id}/metrics" in route_paths
    assert "/stores/{store_id}/funnel" in route_paths
    assert "/stores/{store_id}/insights" in route_paths
    assert "/stores/{store_id}/paths" in route_paths
    assert "/stores/{store_id}/anomalies" in route_paths
    assert "/api/v1/stores/{store_id}/metrics" in route_paths
    assert "/api/v1/stores/{store_id}/funnel" in route_paths
    assert "/api/v1/stores/{store_id}/insights" in route_paths
    assert "/api/v1/stores/{store_id}/paths" in route_paths
    assert "/api/v1/stores/{store_id}/anomalies" in route_paths
    assert "/events/ingest" in route_paths


def test_request_middleware_adds_trace_header() -> None:
    class FakeRequest:
        headers = {}
        url = SimpleNamespace(path="/stores/ST1008/metrics")

        async def body(self) -> bytes:
            return b""

    class FakeResponse:
        status_code = 200

        def __init__(self) -> None:
            self.headers = {}

    async def call_next(request):
        return FakeResponse()

    response = asyncio.run(structured_request_logging(FakeRequest(), call_next))

    assert response.headers["x-trace-id"]


def test_health_returns_structured_503_when_database_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenSession:
        def __enter__(self):
            raise OperationalError("SELECT 1", {}, Exception("database down"))

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(main_module, "SessionLocal", BrokenSession)

    response = asyncio.run(health_check())

    assert response.status_code == 503
    assert b'"status":"degraded"' in response.body
    assert b"DATABASE_UNAVAILABLE" in response.body
