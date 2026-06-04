# PROMPT: Generate tests for POS-to-visit correlation, confidence scoring, ambiguity, and no-match cases.
# CHANGES MADE: Tuned fixtures to deterministic timestamps and verified explanation fields for reviewer traceability.
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import CorrelationStatus, EventType, SessionStatus
from app.models.event import Event
from app.models.pos import PosTransaction, TransactionCorrelation
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession
from app.services.correlation_service import CorrelationService


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


def create_store(db_session: Session, store_id: str = "ST1008") -> Store:
    store = Store(id=store_id, name=None)
    db_session.add(store)
    return store


def create_session(
    db_session: Session,
    *,
    entity_id: str,
    store_id: str = "ST1008",
    entry_time: datetime,
    exit_time: datetime | None = None,
) -> VisitSession:
    entity = TrackedEntity(id=entity_id, store_id=store_id, is_staff=False)
    session = VisitSession(
        tracked_entity_id=entity_id,
        store_id=store_id,
        entry_time=entry_time,
        exit_time=exit_time,
        dwell_seconds=int((exit_time - entry_time).total_seconds()) if exit_time else None,
        session_status=SessionStatus.COMPLETED if exit_time else SessionStatus.IN_PROGRESS,
    )
    db_session.add_all([entity, session])
    db_session.flush()
    return session


def create_queue_event(
    db_session: Session,
    session: VisitSession,
    *,
    timestamp: datetime,
    abandoned: bool = False,
) -> Event:
    event = Event(
        session_id=session.id,
        tracked_entity_id=session.tracked_entity_id,
        store_id=session.store_id,
        camera_id=None,
        zone_id=None,
        event_type=EventType.QUEUE_ABANDONED if abandoned else EventType.QUEUE_COMPLETED,
        timestamp=timestamp,
        hotspot_x=None,
        hotspot_y=None,
        is_face_hidden=None,
        queue_event_id=f"queue-{session.id}",
        queue_join_ts=timestamp - timedelta(minutes=8),
        queue_served_ts=None if abandoned else timestamp - timedelta(minutes=1),
        queue_exit_ts=timestamp,
        wait_seconds=420,
        queue_position_at_join=2,
        abandoned=abandoned,
    )
    db_session.add(event)
    return event


def create_transaction(
    db_session: Session,
    *,
    order_id: str,
    store_id: str = "ST1008",
    timestamp: datetime,
) -> PosTransaction:
    transaction = PosTransaction(order_id=order_id, store_id=store_id, timestamp=timestamp)
    db_session.add(transaction)
    db_session.flush()
    return transaction


def test_successful_match_persists_correlation(db_session: Session) -> None:
    create_store(db_session)
    base_time = datetime(2026, 4, 10, 12, 0, 0)
    session = create_session(
        db_session,
        entity_id="visitor-1",
        entry_time=base_time,
        exit_time=base_time + timedelta(minutes=40),
    )
    create_queue_event(db_session, session, timestamp=base_time + timedelta(minutes=35))
    transaction = create_transaction(
        db_session,
        order_id="order-1",
        timestamp=base_time + timedelta(minutes=37),
    )

    correlations = CorrelationService(db_session).correlate_all()
    db_session.commit()

    assert len(correlations) == 1
    correlation = db_session.get(TransactionCorrelation, correlations[0].id)
    assert correlation is not None
    assert correlation.status == CorrelationStatus.MATCHED
    assert correlation.session_id == session.id
    assert correlation.transaction_id == transaction.id
    assert correlation.confidence_score >= 0.80
    assert correlation.correlation_method == "queue_exit_time"
    assert "queue_seconds_delta" in (correlation.explanation or "")


def test_unmatched_transaction_is_persisted(db_session: Session) -> None:
    create_store(db_session)
    create_transaction(
        db_session,
        order_id="order-1",
        timestamp=datetime(2026, 4, 10, 12, 0, 0),
    )

    CorrelationService(db_session).correlate_all()
    db_session.commit()

    correlation = db_session.scalars(select(TransactionCorrelation)).one()
    assert correlation.status == CorrelationStatus.UNMATCHED
    assert correlation.transaction_id is not None
    assert correlation.session_id is None
    assert correlation.correlation_method == "no_eligible_visitor"


def test_unmatched_visitor_is_persisted(db_session: Session) -> None:
    create_store(db_session)
    session = create_session(
        db_session,
        entity_id="visitor-1",
        entry_time=datetime(2026, 4, 10, 12, 0, 0),
        exit_time=datetime(2026, 4, 10, 12, 30, 0),
    )

    CorrelationService(db_session).correlate_all()
    db_session.commit()

    correlation = db_session.scalars(select(TransactionCorrelation)).one()
    assert correlation.status == CorrelationStatus.UNMATCHED
    assert correlation.session_id == session.id
    assert correlation.transaction_id is None
    assert correlation.correlation_method == "no_eligible_transaction"


def test_ambiguous_match_persists_candidate_correlations(db_session: Session) -> None:
    create_store(db_session)
    base_time = datetime(2026, 4, 10, 12, 0, 0)
    first_session = create_session(
        db_session,
        entity_id="visitor-1",
        entry_time=base_time,
        exit_time=base_time + timedelta(minutes=40),
    )
    second_session = create_session(
        db_session,
        entity_id="visitor-2",
        entry_time=base_time + timedelta(minutes=1),
        exit_time=base_time + timedelta(minutes=40),
    )
    create_queue_event(db_session, first_session, timestamp=base_time + timedelta(minutes=35))
    create_queue_event(db_session, second_session, timestamp=base_time + timedelta(minutes=35))
    create_transaction(
        db_session,
        order_id="order-1",
        timestamp=base_time + timedelta(minutes=36),
    )

    CorrelationService(db_session).correlate_all()
    db_session.commit()

    correlations = db_session.scalars(
        select(TransactionCorrelation).where(TransactionCorrelation.status == CorrelationStatus.AMBIGUOUS)
    ).all()
    assert len(correlations) == 2
    assert {correlation.session_id for correlation in correlations} == {first_session.id, second_session.id}
    assert db_session.scalar(select(func.count()).select_from(TransactionCorrelation)) == 2


def test_confidence_scoring_prefers_close_queue_completion(db_session: Session) -> None:
    create_store(db_session)
    base_time = datetime(2026, 4, 10, 12, 0, 0)
    session = create_session(
        db_session,
        entity_id="visitor-1",
        entry_time=base_time,
        exit_time=base_time + timedelta(minutes=45),
    )
    create_queue_event(db_session, session, timestamp=base_time + timedelta(minutes=30))
    close_transaction = create_transaction(
        db_session,
        order_id="close",
        timestamp=base_time + timedelta(minutes=31),
    )
    far_transaction = create_transaction(
        db_session,
        order_id="far",
        timestamp=base_time + timedelta(hours=2),
    )

    service = CorrelationService(db_session)
    close_score = service.score_candidate(session, close_transaction).confidence_score
    far_score = service.score_candidate(session, far_transaction).confidence_score

    assert close_score > far_score
    assert close_score >= service.minimum_confidence
    assert far_score < service.minimum_confidence
