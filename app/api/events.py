from typing import Any

from fastapi import APIRouter, Depends, status, HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
import structlog

from app.schemas.event import EventPayload
from app.db.session import get_db
from app.models.event import Event
from app.services.event_ingestion_service import EventIngestionService

logger = structlog.get_logger(__name__)
router = APIRouter()
event_adapter = TypeAdapter(EventPayload)

@router.post("/", status_code=status.HTTP_202_ACCEPTED)
def ingest_event(payload: EventPayload, db: Session = Depends(get_db)):
    """Ingest a single CCTV tracking event."""
    service = EventIngestionService(db)
    try:
        new_event = service.process_event(payload)
        db.commit()
        return {
            "status": "accepted",
            "event_type": new_event.event_type.value,
            "tracked_entity_id": new_event.tracked_entity_id,
            "timestamp": new_event.timestamp.isoformat(),
        }
    except Exception as e:
        db.rollback()
        logger.error("event_ingestion_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to process event")


@router.post("/ingest", status_code=status.HTTP_207_MULTI_STATUS)
def ingest_events(payload: list[dict[str, Any]], db: Session = Depends(get_db)):
    """Ingest a batch of up to 500 events with per-item validation and idempotency."""
    if len(payload) > 500:
        raise HTTPException(status_code=413, detail={"error": "batch_too_large", "max_events": 500})

    service = EventIngestionService(db)
    results: list[dict[str, Any]] = []
    accepted = 0
    duplicates = 0
    failed = 0

    for index, raw_event in enumerate(payload):
        source_event_id = raw_event.get("event_id") if isinstance(raw_event, dict) else None
        try:
            if source_event_id and _event_exists(db, source_event_id):
                duplicates += 1
                results.append({"index": index, "status": "duplicate", "event_id": source_event_id})
                continue

            event_payload = event_adapter.validate_python(raw_event)
            event = service.process_event(event_payload)
            db.commit()
            accepted += 1
            results.append(
                {
                    "index": index,
                    "status": "accepted",
                    "event_id": source_event_id or event.source_event_id or event.id,
                    "event_type": event.event_type.value,
                }
            )
        except ValidationError as exc:
            db.rollback()
            failed += 1
            results.append({"index": index, "status": "failed", "error": exc.errors()})
        except Exception as exc:
            db.rollback()
            failed += 1
            logger.warning("batch_event_ingestion_item_failed", index=index, error=str(exc))
            results.append({"index": index, "status": "failed", "error": "Failed to process event"})

    return {
        "status": "partial_success" if failed and (accepted or duplicates) else "accepted" if not failed else "failed",
        "accepted": accepted,
        "duplicates": duplicates,
        "failed": failed,
        "results": results,
    }


def _event_exists(db: Session, source_event_id: str) -> bool:
    return db.scalar(select(Event.id).where(Event.source_event_id == source_event_id)) is not None
