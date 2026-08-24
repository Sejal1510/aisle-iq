# PROMPT: Generate tests for POS CSV ingestion, transaction item grouping, idempotency, and validation behavior.
# CHANGES MADE: Used temporary CSV fixtures and preserved ingestion semantics for duplicate transaction rows.
import csv
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.pos import PosTransaction, PosTransactionItem
from app.models.store import Store
from app.schemas.pos import PosRow
from app.services.pos_ingestion_service import PosIngestionService

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


def load_pos_sample_rows() -> list[dict]:
    with POS_SAMPLE_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def test_pos_row_normalizes_store_and_parses_timestamp() -> None:
    row = PosRow.model_validate(
        {
            "order_id": "1",
            "order_date": "10-04-2026",
            "order_time": "12:15:05",
            "store_id": "store_1008",
            "product_id": "399945",
            "brand_name": "Faces Canada",
            "total_amount": "302.33",
        }
    )

    assert row.store_id == "ST1008"
    assert row.timestamp is not None
    assert row.timestamp.isoformat() == "2026-04-10T12:15:05"
    assert row.total_amount == 302.33


def test_valid_pos_import_persists_transactions_items_and_store(db_session: Session) -> None:
    rows = load_pos_sample_rows()
    result = PosIngestionService(db_session).import_rows(rows)
    db_session.commit()

    assert result.processed_rows == 101
    assert result.imported_transactions == 101
    assert result.imported_items == 101
    assert result.failed_rows == 0
    assert db_session.scalar(select(func.count()).select_from(PosTransaction)) == 101
    assert db_session.scalar(select(func.count()).select_from(PosTransactionItem)) == 101
    assert db_session.get(Store, "ST1008") is not None


def test_duplicate_pos_import_is_idempotent(db_session: Session) -> None:
    rows = load_pos_sample_rows()
    service = PosIngestionService(db_session)

    first_result = service.import_rows(rows)
    db_session.commit()
    second_result = service.import_rows(rows)
    db_session.commit()

    assert first_result.imported_transactions == 101
    assert second_result.imported_transactions == 0
    assert second_result.imported_items == 0
    assert second_result.skipped_transactions == 101
    assert db_session.scalar(select(func.count()).select_from(PosTransaction)) == 101
    assert db_session.scalar(select(func.count()).select_from(PosTransactionItem)) == 101


def test_malformed_rows_are_reported_and_valid_rows_still_import(db_session: Session) -> None:
    rows = [
        {
            "order_id": "A1",
            "order_date": "10-04-2026",
            "order_time": "12:15:05",
            "store_id": "ST1008",
            "product_id": "399945",
            "brand_name": "Faces Canada",
            "total_amount": "302.33",
        },
        {
            "order_id": "BAD",
            "order_date": "2026-04-10",
            "order_time": "12:15:05",
            "store_id": "ST1008",
            "product_id": "399945",
            "brand_name": "Faces Canada",
            "total_amount": "302.33",
        },
        {
            "order_id": "BAD2",
            "order_date": "10-04-2026",
            "order_time": "12:15:05",
            "store_id": "ST1008",
            "product_id": "399945",
            "brand_name": "Faces Canada",
            "total_amount": "not-a-number",
        },
    ]

    result = PosIngestionService(db_session).import_rows(rows)
    db_session.commit()

    assert result.processed_rows == 3
    assert result.imported_transactions == 1
    assert result.imported_items == 1
    assert result.failed_rows == 2
    assert len(result.errors) == 2
    assert db_session.scalar(select(func.count()).select_from(PosTransaction)) == 1
    assert db_session.scalar(select(func.count()).select_from(PosTransactionItem)) == 1


def test_transaction_grouping_uses_store_and_order_id(db_session: Session) -> None:
    rows = [
        {
            "order_id": "ORDER-1",
            "order_date": "10-04-2026",
            "order_time": "12:15:05",
            "store_id": "ST1008",
            "product_id": "P1",
            "brand_name": "Brand A",
            "total_amount": "10.00",
        },
        {
            "order_id": "ORDER-1",
            "order_date": "10-04-2026",
            "order_time": "12:15:05",
            "store_id": "ST1008",
            "product_id": "P2",
            "brand_name": "Brand B",
            "total_amount": "15.50",
        },
        {
            "order_id": "ORDER-1",
            "order_date": "10-04-2026",
            "order_time": "12:15:05",
            "store_id": "ST2001",
            "product_id": "P3",
            "brand_name": "Brand C",
            "total_amount": "8.25",
        },
    ]

    result = PosIngestionService(db_session).import_rows(rows)
    db_session.commit()

    transactions = db_session.scalars(select(PosTransaction).order_by(PosTransaction.store_id)).all()

    assert result.imported_transactions == 2
    assert result.imported_items == 3
    assert len(transactions) == 2
    assert [transaction.store_id for transaction in transactions] == ["ST1008", "ST2001"]
    assert [len(transaction.items) for transaction in transactions] == [2, 1]
