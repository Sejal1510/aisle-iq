# pipeline/ingest_events.py
"""
Batch ingestion script for ``sample_events.jsonl``.

How it works
------------
* Opens the JSON‑Lines file (default location: ``data/sample_events.jsonl``).
* Reads the file line‑by‑line, validates each line with the Pydantic
  ``EventPayload`` discriminated union (the same schema used by the API).
* Delegates persistence to the existing ``EventIngestionService`` – no duplicated logic.
* Logs progress every 1 000 records, batches DB commits every 500 records,
  and reports final timing/throughput metrics.
* Uses ``TypeAdapter`` for validation (compatible with the project’s Pydantic v2).

Run it
------
$ python -m pipeline.ingest_events   # from the project root
"""

import json
import pathlib
import sys
import time
from typing import Tuple

import structlog
from sqlalchemy.orm import Session
from pydantic import TypeAdapter

# Project imports – reuse the same code path as the FastAPI endpoint
from app.db.session import SessionLocal, init_db
from app.schemas.event import EventPayload
from app.services.event_ingestion_service import EventIngestionService

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
DEFAULT_EVENTS_PATH = pathlib.Path(__file__).parent.parent / "data" / "sample_events.jsonl"
BATCH_SIZE = 500               # DB commit batch size
PROGRESS_INTERVAL = 1_000      # Log progress every N records

logger = structlog.get_logger(__name__)

# Prepare a TypeAdapter once – it handles the discriminated Union correctly
_event_adapter = TypeAdapter(EventPayload)


def _process_line(line: str, service: EventIngestionService) -> Tuple[bool, str]:
    """
    Parse a single JSON line and hand it to the service.

    Returns
    -------
    (bool, str)
        *True* and an empty message on success,
        *False* and the error message on failure.
    """
    try:
        payload_dict = json.loads(line.strip())
        # Validation using TypeAdapter (required for a discriminated Union)
        payload = _event_adapter.validate_python(payload_dict)
        service.process_event(payload)  # persistence (no commit here)
        return True, ""
    except Exception as exc:  # pylint: disable=broad-except
        return False, str(exc)


def main(events_path: pathlib.Path = DEFAULT_EVENTS_PATH) -> None:
    """Entry point for the batch importer."""
    # Ensure DB tables exist (same as FastAPI startup)
    init_db()

    # One session per script run – we control the transaction manually
    db: Session = SessionLocal()
    service = EventIngestionService(db)

    success_cnt = 0
    failure_cnt = 0
    processed_cnt = 0
    batch_counter = 0

    start_ts = time.time()

    try:
        with events_path.open("r", encoding="utf-8") as fp:
            for line_no, raw in enumerate(fp, start=1):
                if not raw.strip():
                    continue

                ok, err_msg = _process_line(raw, service)
                processed_cnt += 1
                batch_counter += 1

                if ok:
                    success_cnt += 1
                else:
                    failure_cnt += 1
                    logger.error(
                        "event_ingestion_failed",
                        line=line_no,
                        error=err_msg,
                        raw_line=raw[:200],
                    )

                # ------------------------------------------------------------------
                # Batch commit handling – commit every BATCH_SIZE records
                # ------------------------------------------------------------------
                if batch_counter >= BATCH_SIZE:
                    db.commit()
                    batch_counter = 0

                # ------------------------------------------------------------------
                # Progress logging every PROGRESS_INTERVAL records
                # ------------------------------------------------------------------
                if processed_cnt % PROGRESS_INTERVAL == 0:
                    logger.info(
                        "batch_ingestion_progress",
                        records_processed=processed_cnt,
                        succeeded=success_cnt,
                        failed=failure_cnt,
                    )

        # Commit any remaining records that didn’t fill a full batch
        if batch_counter > 0:
            db.commit()

    except Exception as e:  # Unexpected fatal error (e.g., file not found)
        logger.critical("batch_ingestion_crashed", error=str(e))
        db.rollback()
        raise
    finally:
        db.close()

    duration_sec = time.time() - start_ts
    throughput = processed_cnt / duration_sec if duration_sec > 0 else 0.0

    logger.info(
        "batch_ingestion_complete",
        total_processed=processed_cnt,
        succeeded=success_cnt,
        failed=failure_cnt,
        source_file=str(events_path),
        duration_seconds=round(duration_sec, 2),
        throughput_events_per_sec=round(throughput, 2),
    )


if __name__ == "__main__":
    # Optional CLI override: python -m pipeline.ingest_events path/to/file.jsonl
    path_arg = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_EVENTS_PATH
    main(path_arg)
