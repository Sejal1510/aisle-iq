from typing import Any, Optional

from fastapi import APIRouter, Depends, status, HTTPException
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
import structlog
from structlog.contextvars import get_contextvars

from app.core.security import require_api_key
from app.models.auth import ApiKey
from app.schemas.event import EventPayload
from app.db.session import get_db
from app.models.event import Event
from app.services.event_ingestion_service import EventIngestionService
from app.services.visitor_inference_service import VisitorInferenceService

logger = structlog.get_logger(__name__)
router = APIRouter()
event_adapter = TypeAdapter(EventPayload)


def _error_body(error: str, message: str, details: list | None = None) -> dict:
    return {
        "error": error,
        "message": message,
        "trace_id": get_contextvars().get("trace_id"),
        "details": details or [],
    }


def _extract_store_id(raw_event: Any) -> Optional[str]:
    """Best-effort store id extraction from a not-yet-validated raw payload --
    used for the pre-validation authorization and idempotency-pre-check steps,
    which both need to know the store before (or without) fully parsing the
    event. Field name varies by source schema (store_code for entry/exit,
    store_id for everything else)."""
    if not isinstance(raw_event, dict):
        return None
    return raw_event.get("store_id") or raw_event.get("store_code")


def _authorization_error(store_id: str) -> dict:
    return _error_body(
        "store_not_authorized",
        f"API key is not authorized for store '{store_id}'.",
    )


@router.post("/", status_code=status.HTTP_202_ACCEPTED)
def ingest_event(
    payload: EventPayload,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
):
    """Ingest a single CCTV tracking event.

    Malformed request bodies never reach this function -- FastAPI validates
    ``payload`` against the ``EventPayload`` union first and returns 422
    automatically. From here: the event's store must match the API key's
    authorized store (403), a domain-level rejection is a 400, and anything
    unexpected is a 500. None of these ever include internal exception detail
    in the response body -- that goes to the server log only, keyed by
    trace_id.
    """
    event_store_id = getattr(payload, "store_id", None) or getattr(payload, "store_code", None)
    if event_store_id and event_store_id != api_key.store_id:
        return JSONResponse(status_code=403, content=_authorization_error(event_store_id))

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
    except ValueError as exc:
        db.rollback()
        logger.warning("event_ingestion_invalid", error=str(exc))
        return JSONResponse(status_code=400, content=_error_body("invalid_event", str(exc)))
    except Exception as exc:
        db.rollback()
        logger.exception("event_ingestion_failed", error=str(exc))
        return JSONResponse(
            status_code=500,
            content=_error_body("ingestion_failed", "Failed to process event."),
        )


@router.post("/ingest", status_code=status.HTTP_207_MULTI_STATUS)
def ingest_events(
    payload: list[dict[str, Any]],
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
):
    """Ingest a batch of up to 500 events with per-item validation, per-item
    store authorization, and idempotency.

    Staff/group inference runs once per store touched by the batch, after every
    item has been processed, rather than once per event -- see
    ``EventIngestionService.process_event``'s ``run_inference`` parameter.
    """
    if len(payload) > 500:
        raise HTTPException(status_code=413, detail={"error": "batch_too_large", "max_events": 500})

    service = EventIngestionService(db)
    results: list[dict[str, Any]] = []
    accepted = 0
    duplicates = 0
    failed = 0
    touched_store_ids: set[str] = set()

    for index, raw_event in enumerate(payload):
        source_event_id = raw_event.get("event_id") if isinstance(raw_event, dict) else None
        event_store_id = _extract_store_id(raw_event)

        try:
            if event_store_id and event_store_id != api_key.store_id:
                failed += 1
                results.append(
                    {"index": index, "status": "failed", "error": _authorization_error(event_store_id)["message"]}
                )
                continue

            if source_event_id and event_store_id and _event_exists(db, event_store_id, source_event_id):
                duplicates += 1
                results.append({"index": index, "status": "duplicate", "event_id": source_event_id})
                continue

            event_payload = event_adapter.validate_python(raw_event)
            event = service.process_event(event_payload, run_inference=False)
            db.commit()
            accepted += 1
            touched_store_ids.add(event.store_id)
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

    for store_id in touched_store_ids:
        VisitorInferenceService(db).infer_store(store_id)
    if touched_store_ids:
        db.commit()

    return {
        "status": "partial_success" if failed and (accepted or duplicates) else "accepted" if not failed else "failed",
        "accepted": accepted,
        "duplicates": duplicates,
        "failed": failed,
        "results": results,
    }


def _event_exists(db: Session, store_id: str, source_event_id: str) -> bool:
    return (
        db.scalar(
            select(Event.id).where(Event.store_id == store_id, Event.source_event_id == source_event_id)
        )
        is not None
    )
