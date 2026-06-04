# PROMPT: Generate tests for deterministic retail insights covering queue, zone, conversion, visitor, and revenue rules.
# CHANGES MADE: Kept rule thresholds explicit and used seeded analytics data rather than LLM-generated recommendations.
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.services.insights_service import InsightsService
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


def test_insights_service_generates_deterministic_recommendations(db_session: Session) -> None:
    response = InsightsService(db_session).get_store_insights("ST1008")

    assert response.store_id == "ST1008"
    titles = [insight.title for insight in response.insights]
    assert "Queue Abandonment Risk" in titles
    assert "Long Queue Wait Time" in titles
    assert "Funnel Drop-Off: Queue Complete" in titles
    assert "High Group Traffic" in titles
    assert "High Staff-To-Customer Ratio" in titles
    assert "High Dwell Zone: makeup" in titles

    queue_insight = next(insight for insight in response.insights if insight.title == "Queue Abandonment Risk")
    assert queue_insight.severity == "HIGH"
    assert "50%" in queue_insight.explanation
    assert "billing counter" in queue_insight.recommendation


def test_insights_are_sorted_by_severity(db_session: Session) -> None:
    response = InsightsService(db_session).get_store_insights("ST1008")
    severities = [insight.severity for insight in response.insights]

    assert severities == sorted(severities, key={"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get)
