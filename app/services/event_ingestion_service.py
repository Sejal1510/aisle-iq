from sqlalchemy.orm import Session
from sqlalchemy import select
import structlog

from app.schemas.event import EventPayload, EntryEvent, ExitEvent, ZoneEnteredEvent, ZoneExitedEvent
from app.models.tracking import TrackedEntity, VisitSession
from app.models.event import Event
from app.models.enums import EventType, SessionStatus

logger = structlog.get_logger(__name__)

class EventIngestionService:
    def __init__(self, db: Session):
        self.db = db

    def process_event(self, payload: EventPayload) -> Event:
        logger.info("processing_event", event_type=payload.event_type)

        # 1. Normalize schema disparities between entry/exit and zone events
        if isinstance(payload, (EntryEvent, ExitEvent)):
            person_id = payload.id_token
            store_id = payload.store_code
            timestamp = payload.event_timestamp
            gender = payload.gender_pred
            age = payload.age_pred
            age_bucket = payload.age_bucket
            is_staff = payload.is_staff
            group_id = payload.group_id
            group_size = payload.group_size
        else:
            person_id = payload.track_id
            store_id = payload.store_id
            timestamp = payload.event_time
            gender = payload.gender
            age = payload.age
            age_bucket = payload.age_bucket
            is_staff = False
            group_id = None
            group_size = None

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
                group_id=group_id,
                group_size=group_size,
            )
            self.db.add(entity)

        # 3. Get or Create VisitSession
        session = self.db.execute(
            select(VisitSession).where(
                VisitSession.tracked_entity_id == person_id,
                VisitSession.store_id == store_id,
                VisitSession.session_status == SessionStatus.IN_PROGRESS,
            )
        ).scalar_one_or_none()

        if not session:
            session = VisitSession(
                tracked_entity_id=person_id,
                store_id=store_id,
                entry_time=timestamp,
                session_status=SessionStatus.IN_PROGRESS,
            )
            self.db.add(session)
            self.db.flush()  # Ensure session.id is available for Event FK

        # 4. Append Event Record
        db_event_type = EventType(payload.event_type)
        zone_id = payload.zone_id if isinstance(payload, (ZoneEnteredEvent, ZoneExitedEvent)) else None
        hotspot_x = payload.zone_hotspot_x if isinstance(payload, (ZoneEnteredEvent, ZoneExitedEvent)) else None
        hotspot_y = payload.zone_hotspot_y if isinstance(payload, (ZoneEnteredEvent, ZoneExitedEvent)) else None
        is_face_hidden = payload.is_face_hidden if isinstance(payload, (EntryEvent, ExitEvent)) else None

        new_event = Event(
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
        )
        self.db.add(new_event)

        # 5. Close Session on Exit
        if db_event_type == EventType.EXIT:
            session.exit_time = timestamp
            session.session_status = SessionStatus.COMPLETED
            delta = timestamp - session.entry_time
            session.dwell_seconds = int(delta.total_seconds())
            logger.info("session_completed", session_id=session.id, dwell_seconds=session.dwell_seconds)

        self.db.flush()  # Push changes before returning
        return new_event
