from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy.orm import Session
import structlog

from app.schemas.event import EventPayload
from app.db.session import get_db
from app.services.event_ingestion_service import EventIngestionService

logger = structlog.get_logger(__name__)
router = APIRouter()

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
