import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import select
import structlog

from app.schemas.event import (
    CanonicalEvent,
    CanonicalEventType,
    EventPayload,
    EntryEvent,
    ExitEvent,
    QueueAbandonedEvent,
    QueueCompletedEvent,
    ReentryEvent,
    ZoneEnteredEvent,
    ZoneExitedEvent,
)
from app.models.tracking import TrackedEntity, VisitSession
from app.models.event import Event
from app.models.enums import EventType, SessionStatus
from app.services.visitor_inference_service import VisitorInferenceService

logger = structlog.get_logger(__name__)

class EventIngestionService:
    def __init__(self, db: Session):
        self.db = db

    def process_event(self, payload: EventPayload) -> Event:
        logger.info("processing_event", event_type=payload.event_type)

        source_event_id = getattr(payload, "event_id", None)
        if source_event_id:
            existing_event = self.db.execute(
                select(Event).where(Event.source_event_id == source_event_id)
            ).scalar_one_or_none()
            if existing_event is not None:
                logger.info("duplicate_event_ignored", event_id=source_event_id)
                return existing_event

        # 1. Normalize schema disparities between source event families.
        if isinstance(payload, CanonicalEvent):
            normalized = self._normalize_canonical_event(payload)
            person_id = normalized["person_id"]
            store_id = normalized["store_id"]
            timestamp = normalized["timestamp"]
            gender = None
            age = None
            age_bucket = None
            is_staff = payload.is_staff
            group_id = payload.metadata.get("group_id")
            group_size = payload.metadata.get("group_size")
            zone_id = normalized["zone_id"]
            hotspot_x = payload.metadata.get("hotspot_x")
            hotspot_y = payload.metadata.get("hotspot_y")
            is_face_hidden = payload.metadata.get("is_face_hidden")
            db_event_type = normalized["event_type"]
        elif isinstance(payload, (EntryEvent, ExitEvent, ReentryEvent)):
            person_id = payload.id_token
            store_id = payload.store_code
            timestamp = payload.event_timestamp
            gender = payload.gender_pred
            age = payload.age_pred
            age_bucket = payload.age_bucket
            is_staff = payload.is_staff
            group_id = payload.group_id
            group_size = payload.group_size
            zone_id = None
            hotspot_x = None
            hotspot_y = None
            is_face_hidden = payload.is_face_hidden
            db_event_type = EventType(payload.event_type)
        elif isinstance(payload, (ZoneEnteredEvent, ZoneExitedEvent, QueueCompletedEvent, QueueAbandonedEvent)):
            person_id = payload.track_id
            store_id = payload.store_id
            timestamp = payload.event_time if isinstance(payload, (ZoneEnteredEvent, ZoneExitedEvent)) else payload.queue_exit_ts
            gender = payload.gender
            age = payload.age
            age_bucket = payload.age_bucket
            is_staff = False
            group_id = None
            group_size = None
            zone_id = payload.zone_id
            hotspot_x = payload.zone_hotspot_x
            hotspot_y = payload.zone_hotspot_y
            is_face_hidden = None
            db_event_type = EventType(payload.event_type)
        else:
            raise ValueError(f"Unsupported event payload type: {type(payload).__name__}")

        # 2. Get or Create TrackedEntity
        entity = self.db.execute(
            select(TrackedEntity).where(TrackedEntity.id == person_id)
        ).scalar_one_or_none()

        if not entity:
            entity = TrackedEntity(
                id=person_id,
                store_id=store_id,
                gender=gender,
                age=age,
                age_bucket=age_bucket,
                is_staff=is_staff,
                staff_confidence_score=1.0 if is_staff else 0.0,
                staff_inference_reason="source marked as staff" if is_staff else None,
                group_id=group_id,
                group_size=group_size,
            )
            self.db.add(entity)
        elif is_staff:
            entity.is_staff = True
            entity.staff_confidence_score = 1.0
            entity.staff_inference_reason = "source marked as staff"

        # 3. Get or Create VisitSession
        session = self.db.execute(
            select(VisitSession).where(
                VisitSession.tracked_entity_id == person_id,
                VisitSession.store_id == store_id,
                VisitSession.session_status == SessionStatus.IN_PROGRESS,
            )
        ).scalar_one_or_none()

        if not session:
            previous_completed_session = self._previous_completed_session(person_id, store_id, timestamp)
            session = VisitSession(
                tracked_entity_id=person_id,
                store_id=store_id,
                entry_time=timestamp,
                session_status=SessionStatus.IN_PROGRESS,
            )
            self.db.add(session)
            self.db.flush()  # Ensure session.id is available for Event FK
        else:
            previous_completed_session = None

        # 4. Append Event Record
        queue_event_id = None
        queue_join_ts = None
        queue_served_ts = None
        queue_exit_ts = None
        wait_seconds = None
        queue_position_at_join = None
        abandoned = None

        if isinstance(payload, (QueueCompletedEvent, QueueAbandonedEvent)):
            queue_event_id = payload.queue_event_id
            queue_join_ts = payload.queue_join_ts
            queue_served_ts = payload.queue_served_ts
            queue_exit_ts = payload.queue_exit_ts
            wait_seconds = payload.wait_seconds
            queue_position_at_join = payload.queue_position_at_join
            abandoned = payload.abandoned
        elif isinstance(payload, CanonicalEvent):
            queue_event_id = payload.metadata.get("queue_event_id")
            queue_join_ts = _parse_datetime(payload.metadata.get("queue_join_ts"))
            queue_served_ts = _parse_datetime(payload.metadata.get("queue_served_ts"))
            queue_exit_ts = _parse_datetime(payload.metadata.get("queue_exit_ts"))
            wait_seconds = int(payload.dwell_ms / 1000) if payload.dwell_ms and db_event_type in {
                EventType.ZONE_DWELL,
                EventType.QUEUE_ABANDONED,
                EventType.BILLING_QUEUE_JOIN,
            } else payload.metadata.get("wait_seconds")
            queue_position_at_join = payload.metadata.get("queue_depth")
            abandoned = db_event_type == EventType.QUEUE_ABANDONED

        new_event = Event(
            source_event_id=source_event_id,
            session_id=session.id,
            tracked_entity_id=person_id,
            store_id=store_id,
            camera_id=payload.camera_id,
            zone_id=zone_id,
            event_type=db_event_type,
            timestamp=timestamp,
            hotspot_x=hotspot_x,
            hotspot_y=hotspot_y,
            is_face_hidden=is_face_hidden,
            confidence=getattr(payload, "confidence", None),
            metadata_json=json.dumps(getattr(payload, "metadata", None) or {}, separators=(",", ":")),
            queue_event_id=queue_event_id,
            queue_join_ts=queue_join_ts,
            queue_served_ts=queue_served_ts,
            queue_exit_ts=queue_exit_ts,
            wait_seconds=wait_seconds,
            queue_position_at_join=queue_position_at_join,
            abandoned=abandoned,
        )
        self.db.add(new_event)

        # 5. Close Session on Exit
        if db_event_type == EventType.EXIT:
            session.exit_time = timestamp
            session.session_status = SessionStatus.COMPLETED
            delta = timestamp - session.entry_time
            session.dwell_seconds = int(delta.total_seconds())
            logger.info("session_completed", session_id=session.id, dwell_seconds=session.dwell_seconds)

        if (
            previous_completed_session is not None
            and db_event_type == EventType.ENTRY
            and isinstance(payload, (EntryEvent, CanonicalEvent))
        ):
            reentry_event = Event(
                source_event_id=f"{source_event_id}:reentry" if source_event_id else None,
                session_id=session.id,
                tracked_entity_id=person_id,
                store_id=store_id,
                camera_id=payload.camera_id,
                zone_id=zone_id,
                event_type=EventType.REENTRY,
                timestamp=timestamp,
                hotspot_x=hotspot_x,
                hotspot_y=hotspot_y,
                is_face_hidden=is_face_hidden,
                confidence=getattr(payload, "confidence", None),
                metadata_json=json.dumps(
                    {
                        "previous_session_id": previous_completed_session.id,
                        "previous_exit_time": previous_completed_session.exit_time.isoformat()
                        if previous_completed_session.exit_time
                        else None,
                    },
                    separators=(",", ":"),
                ),
            )
            self.db.add(reentry_event)

        self.db.flush()  # Push changes before inference and return.
        VisitorInferenceService(self.db).infer_store(store_id)
        return new_event

    def _previous_completed_session(
        self,
        person_id: str,
        store_id: str,
        timestamp,
        *,
        reentry_window: timedelta = timedelta(days=365),
    ) -> VisitSession | None:
        return self.db.execute(
            select(VisitSession)
            .where(VisitSession.tracked_entity_id == person_id)
            .where(VisitSession.store_id == store_id)
            .where(VisitSession.session_status == SessionStatus.COMPLETED)
            .where(VisitSession.exit_time.is_not(None))
            .where(VisitSession.exit_time <= timestamp)
            .where(VisitSession.exit_time >= timestamp - reentry_window)
            .order_by(VisitSession.exit_time.desc())
        ).scalar_one_or_none()

    @staticmethod
    def _normalize_canonical_event(payload: CanonicalEvent) -> dict:
        event_type_map = {
            CanonicalEventType.ENTRY: EventType.ENTRY,
            CanonicalEventType.EXIT: EventType.EXIT,
            CanonicalEventType.ZONE_ENTER: EventType.ZONE_ENTERED,
            CanonicalEventType.ZONE_EXIT: EventType.ZONE_EXITED,
            CanonicalEventType.ZONE_DWELL: EventType.ZONE_DWELL,
            CanonicalEventType.BILLING_QUEUE_JOIN: EventType.BILLING_QUEUE_JOIN,
            CanonicalEventType.BILLING_QUEUE_ABANDON: EventType.QUEUE_ABANDONED,
            CanonicalEventType.REENTRY: EventType.REENTRY,
        }
        return {
            "person_id": payload.visitor_id,
            "store_id": payload.store_id,
            "timestamp": payload.timestamp,
            "zone_id": payload.zone_id,
            "event_type": event_type_map[payload.event_type],
        }


def _parse_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
