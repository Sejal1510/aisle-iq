from __future__ import annotations

import csv
import pathlib
import sys
import time

import structlog
from sqlalchemy.orm import Session

from app.db.session import SessionLocal, init_db
from app.services.pos_ingestion_service import PosIngestionService

DEFAULT_POS_PATH = pathlib.Path(__file__).parent.parent / "data" / "POS - sample transactionsb1e826f (1).csv"

logger = structlog.get_logger(__name__)


def _load_csv_rows(pos_path: pathlib.Path) -> list[dict]:
    with pos_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def main(pos_path: pathlib.Path = DEFAULT_POS_PATH) -> None:
    init_db()

    db: Session = SessionLocal()
    start_ts = time.time()

    try:
        rows = _load_csv_rows(pos_path)
        result = PosIngestionService(db).import_rows(rows)
        db.commit()
    except Exception as exc:
        logger.critical("pos_ingestion_crashed", error=str(exc))
        db.rollback()
        raise
    finally:
        db.close()

    duration_sec = time.time() - start_ts
    logger.info(
        "pos_ingestion_complete",
        source_file=str(pos_path),
        processed_rows=result.processed_rows,
        imported_transactions=result.imported_transactions,
        imported_items=result.imported_items,
        skipped_transactions=result.skipped_transactions,
        failed_rows=result.failed_rows,
        duration_seconds=round(duration_sec, 2),
    )

    for error in result.errors:
        logger.warning("pos_ingestion_row_error", error=error)


if __name__ == "__main__":
    path_arg = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_POS_PATH
    main(path_arg)
