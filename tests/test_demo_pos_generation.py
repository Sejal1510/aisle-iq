# PROMPT: Generate tests for deterministic demo POS generation aligned with generated CCTV checkout sessions.
# CHANGES MADE: Preserved CSV assertions and added deterministic fixture setup for repeatable correlation behavior.
import csv
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import CorrelationStatus, EventType, SessionStatus
from app.models.event import Event
from app.models.pos import PosTransaction, TransactionCorrelation
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession
from app.services.analytics_service import AnalyticsService
from app.services.correlation_service import CorrelationService
from app.services.pos_ingestion_service import PosIngestionService
from pipeline.generate_demo_pos import generate_demo_pos


POS_SAMPLE_PATH = Path(__file__).parent / "fixtures" / "pos_sample.csv"


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


def seed_demo_sessions(db_session: Session) -> None:
    base_time = datetime(2026, 6, 1, 10, 0, 0)

    for store_id in ("ST1001", "ST1002"):
        db_session.add(Store(id=store_id, name=None))
        for index in range(1, 5):
            entity = TrackedEntity(
                id=f"{store_id}-visitor-{index}",
                store_id=store_id,
                is_staff=False,
            )
            session = VisitSession(
                tracked_entity_id=entity.id,
                store_id=store_id,
                entry_time=base_time + timedelta(seconds=index * 3),
                exit_time=None,
                dwell_seconds=None,
                session_status=SessionStatus.IN_PROGRESS,
            )
            db_session.add_all([entity, session])

    db_session.flush()


def load_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def test_demo_pos_generation_writes_fixture_and_demo_checkout_events(
    db_session: Session,
    tmp_path: Path,
) -> None:
    seed_demo_sessions(db_session)
    output_path = tmp_path / "demo_pos.csv"

    result = generate_demo_pos(
        db_session,
        output_path=output_path,
        product_source_path=POS_SAMPLE_PATH,
        store_targets={"ST1001": 2, "ST1002": 2},
    )
    db_session.commit()

    rows = load_rows(output_path)

    assert output_path.is_file()
    assert result.transaction_counts_by_store == {"ST1001": 2, "ST1002": 2}
    assert len({row["order_id"] for row in rows}) == 4
    assert {row["store_id"] for row in rows} == {"ST1001", "ST1002"}
    assert all(row["order_id"].startswith("DEMO-") for row in rows)
    assert db_session.scalar(
        select(func.count()).select_from(Event).where(Event.event_type == EventType.QUEUE_COMPLETED)
    ) == 4


def test_demo_pos_fixture_ingests_and_produces_business_metrics(
    db_session: Session,
    tmp_path: Path,
) -> None:
    seed_demo_sessions(db_session)
    output_path = tmp_path / "demo_pos.csv"

    generation_result = generate_demo_pos(
        db_session,
        output_path=output_path,
        product_source_path=POS_SAMPLE_PATH,
        store_targets={"ST1001": 3, "ST1002": 2},
    )
    db_session.commit()

    import_result = PosIngestionService(db_session).import_rows(load_rows(output_path))
    CorrelationService(db_session).correlate_all(replace_existing=True)
    db_session.commit()

    matched_count = db_session.scalar(
        select(func.count(func.distinct(TransactionCorrelation.transaction_id)))
        .where(TransactionCorrelation.status == CorrelationStatus.MATCHED)
        .where(TransactionCorrelation.transaction_id.is_not(None))
    )
    st1001_metrics = AnalyticsService(db_session).get_store_metrics("ST1001")
    st1002_metrics = AnalyticsService(db_session).get_store_metrics("ST1002")

    assert import_result.imported_transactions == generation_result.transaction_count
    assert db_session.scalar(select(func.count()).select_from(PosTransaction)) == 5
    assert matched_count == 5
    assert st1001_metrics.attributed_transactions == 3
    assert st1002_metrics.attributed_transactions == 2
    assert st1001_metrics.conversion_rate > 0
    assert st1002_metrics.conversion_rate > 0
    assert st1001_metrics.attributed_revenue > 0
    assert st1002_metrics.attributed_revenue > 0
