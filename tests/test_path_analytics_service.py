# PROMPT: Generate tests for customer path analytics, purchase journeys, and drop-off path aggregation.
# CHANGES MADE: Kept fixtures deterministic and asserted path-level conversion behavior from persisted facts.
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.services.path_analytics_service import PathAnalyticsService
from tests.test_analytics_service import seed_analytics_data


@pytest.fixture()
def db_session() -> Session:
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


def test_path_analytics_reconstructs_customer_journeys(db_session: Session) -> None:
    response = PathAnalyticsService(db_session).get_store_paths("ST1008")

    assert response.store_id == "ST1008"
    assert response.total_journeys >= 1
    makeup_path = next(metric for metric in response.most_common_paths if metric.path == ["makeup"])
    assert makeup_path.visitor_count == 1
    assert makeup_path.purchase_count == 1
    assert makeup_path.conversion_rate == 1.0


def test_path_analytics_reports_drop_off_paths(db_session: Session) -> None:
    response = PathAnalyticsService(db_session).get_store_paths("ST1008")

    assert all(metric.abandonment_count >= 0 for metric in response.path_drop_offs)
    assert response.top_purchase_journeys[0].purchase_count == 1
