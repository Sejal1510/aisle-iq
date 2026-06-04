# PROMPT: Generate service-level tests for store metrics, funnel conversion, staff exclusion, groups, and queue analytics.
# CHANGES MADE: Kept deterministic SQLAlchemy fixtures and hand-checked expected metric calculations.
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import CorrelationStatus, EventType, SessionStatus
from app.models.event import Event
from app.models.pos import PosTransaction, PosTransactionItem, TransactionCorrelation
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession
from app.services.analytics_service import AnalyticsService


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def seed_analytics_data(db_session: Session) -> dict[str, str]:
    store_id = "ST1008"
    base_time = datetime(2026, 4, 10, 12, 0, 0)
    db_session.add(Store(id=store_id, name=None))

    visitor_1 = TrackedEntity(id="visitor-1", store_id=store_id, is_staff=False)
    visitor_2 = TrackedEntity(id="visitor-2", store_id=store_id, is_staff=False, group_id="G1", group_size=2)
    visitor_3 = TrackedEntity(id="visitor-3", store_id=store_id, is_staff=False, group_id="G1", group_size=2)
    staff = TrackedEntity(id="staff-1", store_id=store_id, is_staff=True, staff_confidence_score=0.9)
    db_session.add_all([visitor_1, visitor_2, visitor_3, staff])

    session_1 = VisitSession(
        tracked_entity_id=visitor_1.id,
        store_id=store_id,
        entry_time=base_time,
        exit_time=base_time + timedelta(minutes=30),
        dwell_seconds=1800,
        session_status=SessionStatus.COMPLETED,
    )
    session_2 = VisitSession(
        tracked_entity_id=visitor_2.id,
        store_id=store_id,
        entry_time=base_time + timedelta(minutes=5),
        exit_time=base_time + timedelta(minutes=20),
        dwell_seconds=900,
        session_status=SessionStatus.COMPLETED,
    )
    staff_session = VisitSession(
        tracked_entity_id=staff.id,
        store_id=store_id,
        entry_time=base_time,
        exit_time=base_time + timedelta(hours=1),
        dwell_seconds=3600,
        session_status=SessionStatus.COMPLETED,
    )
    session_3 = VisitSession(
        tracked_entity_id=visitor_3.id,
        store_id=store_id,
        entry_time=base_time + timedelta(minutes=6),
        exit_time=base_time + timedelta(minutes=16),
        dwell_seconds=600,
        session_status=SessionStatus.COMPLETED,
    )
    db_session.add_all([session_1, session_2, session_3, staff_session])
    db_session.flush()

    db_session.add_all(
        [
            Event(
                session_id=session_1.id,
                tracked_entity_id=visitor_1.id,
                store_id=store_id,
                camera_id=None,
                zone_id="makeup",
                event_type=EventType.ZONE_ENTERED,
                timestamp=base_time + timedelta(minutes=2),
                hotspot_x=None,
                hotspot_y=None,
                is_face_hidden=None,
            ),
            Event(
                session_id=session_1.id,
                tracked_entity_id=visitor_1.id,
                store_id=store_id,
                camera_id=None,
                zone_id="makeup",
                event_type=EventType.ZONE_EXITED,
                timestamp=base_time + timedelta(minutes=7),
                hotspot_x=None,
                hotspot_y=None,
                is_face_hidden=None,
            ),
            Event(
                session_id=session_1.id,
                tracked_entity_id=visitor_1.id,
                store_id=store_id,
                camera_id=None,
                zone_id=None,
                event_type=EventType.QUEUE_COMPLETED,
                timestamp=base_time + timedelta(minutes=25),
                hotspot_x=None,
                hotspot_y=None,
                is_face_hidden=None,
                queue_join_ts=base_time + timedelta(minutes=15),
                queue_served_ts=base_time + timedelta(minutes=24),
                queue_exit_ts=base_time + timedelta(minutes=25),
                wait_seconds=540,
                abandoned=False,
            ),
            Event(
                session_id=session_2.id,
                tracked_entity_id=visitor_2.id,
                store_id=store_id,
                camera_id=None,
                zone_id=None,
                event_type=EventType.QUEUE_ABANDONED,
                timestamp=base_time + timedelta(minutes=18),
                hotspot_x=None,
                hotspot_y=None,
                is_face_hidden=None,
                queue_join_ts=base_time + timedelta(minutes=10),
                queue_exit_ts=base_time + timedelta(minutes=18),
                wait_seconds=480,
                abandoned=True,
            ),
        ]
    )

    transaction = PosTransaction(order_id="order-1", store_id=store_id, timestamp=base_time + timedelta(minutes=26))
    db_session.add(transaction)
    db_session.flush()
    db_session.add_all(
        [
            PosTransactionItem(transaction_id=transaction.id, product_id="P1", brand_name="Brand A", amount=100.0),
            PosTransactionItem(transaction_id=transaction.id, product_id="P2", brand_name="Brand B", amount=50.0),
            TransactionCorrelation(
                transaction_id=transaction.id,
                session_id=session_1.id,
                status=CorrelationStatus.MATCHED,
                confidence_score=0.9,
                correlation_method="queue_exit_time",
                explanation=None,
            ),
        ]
    )
    db_session.flush()
    return {"store_id": store_id}


def test_store_metrics_calculate_business_intelligence(db_session: Session) -> None:
    seeded = seed_analytics_data(db_session)

    metrics = AnalyticsService(db_session).get_store_metrics(seeded["store_id"])

    assert metrics.total_visitors == 3
    assert metrics.unique_visitors == 3
    assert metrics.staff_visitors == 1
    assert metrics.solo_visitors == 1
    assert metrics.groups == 1
    assert metrics.average_group_size == 2.0
    assert metrics.conversion_rate == 0.3333
    assert metrics.average_dwell_seconds == 1100.0
    assert metrics.queue_abandonment_rate == 0.5
    assert metrics.average_queue_wait_seconds == 510.0
    assert metrics.attributed_revenue == 150.0
    assert metrics.attributed_transactions == 1
    assert len(metrics.zone_dwell_metrics) == 1
    assert metrics.zone_dwell_metrics[0].zone_id == "makeup"
    assert metrics.zone_dwell_metrics[0].average_dwell_seconds == 300.0


def test_store_funnel_calculates_step_counts_and_rates(db_session: Session) -> None:
    seeded = seed_analytics_data(db_session)

    funnel = AnalyticsService(db_session).get_store_funnel(seeded["store_id"])

    assert funnel.visitors == 3
    assert funnel.queue_join == 2
    assert funnel.queue_complete == 1
    assert funnel.purchase == 1
    assert [step.step for step in funnel.steps] == ["visitors", "queue_join", "queue_complete", "purchase"]
    assert [step.count for step in funnel.steps] == [3, 2, 1, 1]
    assert funnel.steps[2].rate_from_previous == 0.5
    assert funnel.steps[3].rate_from_visitors == 0.3333
