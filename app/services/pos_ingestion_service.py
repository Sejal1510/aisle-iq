from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.pos import PosTransaction, PosTransactionItem
from app.schemas.pos import PosImportResult, PosRow
from app.services.reference_data_service import ReferenceDataService

logger = structlog.get_logger(__name__)


class PosIngestionService:
    def __init__(self, db: Session):
        self.db = db
        self.reference_data = ReferenceDataService(db)
        self._ensured_store_ids: set[str] = set()

    def import_rows(self, raw_rows: Iterable[dict]) -> PosImportResult:
        result = PosImportResult()
        grouped_rows: dict[tuple[str, str], list[PosRow]] = defaultdict(list)

        for row_number, raw_row in enumerate(raw_rows, start=1):
            result.processed_rows += 1
            try:
                row = PosRow.model_validate(raw_row)
            except ValidationError as exc:
                result.failed_rows += 1
                message = f"row {row_number}: {exc.errors()[0]['msg']}"
                result.errors.append(message)
                logger.warning("pos_row_validation_failed", row=row_number, error=message)
                continue

            grouped_rows[(row.store_id, row.order_id)].append(row)

        for (store_id, order_id), rows in grouped_rows.items():
            if self._transaction_exists(store_id=store_id, order_id=order_id):
                result.skipped_transactions += 1
                continue

            self._ensure_store(store_id)

            first_row = rows[0]
            transaction = PosTransaction(
                order_id=order_id,
                store_id=store_id,
                timestamp=first_row.timestamp,
            )
            transaction.items = [
                PosTransactionItem(
                    product_id=row.product_id,
                    brand_name=row.brand_name,
                    amount=row.total_amount,
                )
                for row in rows
            ]
            self.db.add(transaction)
            result.imported_transactions += 1
            result.imported_items += len(rows)

        self.db.flush()
        return result

    def _transaction_exists(self, store_id: str, order_id: str) -> bool:
        return (
            self.db.execute(
                select(PosTransaction.id).where(
                    PosTransaction.store_id == store_id,
                    PosTransaction.order_id == order_id,
                )
            ).first()
            is not None
        )

    def _ensure_store(self, store_id: str) -> None:
        if store_id in self._ensured_store_ids:
            return

        self.reference_data.ensure_store(store_id)
        self._ensured_store_ids.add(store_id)
