import json
import uuid
from datetime import datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import EventType, SessionStatus
from app.models.event import Event
from app.models.raw_event import RawEvent
from app.models.tracking import (
    STORE_SCOPED_CAMERA_ID,
    IdentityAlias,
    TrackedEntity,
    VisitSession,
)
from app.schemas.event import (
    CanonicalEvent,
    CanonicalEventType,
    EntryEvent,
    EventPayload,
    ExitEvent,
    QueueAbandonedEvent,
    QueueCompletedEvent,
    ReentryEvent,
    ZoneEnteredEvent,
    ZoneExitedEvent,
)
from app.services.reference_data_service import ReferenceDataService
from app.services.visitor_inference_service import VisitorInferenceService

logger = structlog.get_logger(__name__)

class EventIngestionService:
    def __init__(self, db: Session):
        self.db = db
        self.reference_data = ReferenceDataService(db)

    def process_event(
        self, payload: EventPayload, *, run_inference: bool = True, replay_job_id: str | None = None
    ) -> Event:
        """Persist a single normalized event.

        ``run_inference`` controls whether staff/group inference runs synchronously
        for this call. It defaults to ``True`` so single-event ingestion keeps its
        existing behavior. Batch callers (the ``/events/ingest`` route and the
        offline JSONL importer) pass ``run_inference=False`` and instead call
        ``VisitorInferenceService.infer_store`` once after the whole batch, so a
        batch of N events triggers one store-history rescan instead of N.

        ``replay_job_id`` is set by ``ReplayService`` (never by live ingestion) --
        it tags the ``RawEvent`` receipt this call writes, and, if this call
        creates a genuinely new ``Event`` (not a duplicate of one already
        persisted), that new ``Event`` too. A replay that only rediscovers
        already-processed events leaves their ``is_replay``/``replay_job_id``
        untouched -- provenance is set once, at first creation, never rewritten.
        """
        logger.info("processing_event", event_type=payload.event_type)

        # 1. Normalize schema disparities between source event families.
        #
        # ``source_field``/``identity_camera_scoped`` describe which identity
        # namespace this event's raw identifier belongs to (see
        # _resolve_tracked_entity): id_token is stable across a store's whole
        # entry system, while track_id/visitor_id values are camera-local.
        if isinstance(payload, CanonicalEvent):
            normalized = self._normalize_canonical_event(payload)
            raw_identity_value = normalized["person_id"]
            source_field = "visitor_id"
            identity_camera_scoped = True
            store_id = normalized["store_id"]
            timestamp = normalized["timestamp"]
            gender = None
            age = None
            age_bucket = None
            is_staff = payload.is_staff
            group_id = payload.metadata.get("group_id")
            group_size = payload.metadata.get("group_size")
            zone_id = normalized["zone_id"]
            zone_name = payload.metadata.get("sku_zone")
            zone_type = payload.metadata.get("zone_type")
            is_revenue_zone = _yes_no_to_bool(payload.metadata.get("is_revenue_zone"))
            hotspot_x = payload.metadata.get("hotspot_x")
            hotspot_y = payload.metadata.get("hotspot_y")
            is_face_hidden = payload.metadata.get("is_face_hidden")
            db_event_type = normalized["event_type"]
        elif isinstance(payload, (EntryEvent, ExitEvent, ReentryEvent)):
            raw_identity_value = payload.id_token
            source_field = "id_token"
            identity_camera_scoped = False
            store_id = payload.store_code
            timestamp = payload.event_timestamp
            gender = payload.gender_pred
            age = payload.age_pred
            age_bucket = payload.age_bucket
            is_staff = payload.is_staff
            group_id = payload.group_id
            group_size = payload.group_size
            zone_id = None
            zone_name = None
            zone_type = None
            is_revenue_zone = None
            hotspot_x = None
            hotspot_y = None
            is_face_hidden = payload.is_face_hidden
            db_event_type = EventType(payload.event_type)
        elif isinstance(payload, (ZoneEnteredEvent, ZoneExitedEvent, QueueCompletedEvent, QueueAbandonedEvent)):
            raw_identity_value = payload.track_id
            source_field = "track_id"
            identity_camera_scoped = True
            store_id = payload.store_id
            timestamp = payload.event_time if isinstance(payload, (ZoneEnteredEvent, ZoneExitedEvent)) else payload.queue_exit_ts
            gender = payload.gender
            age = payload.age
            age_bucket = payload.age_bucket
            is_staff = False
            group_id = None
            group_size = None
            zone_id = payload.zone_id
            zone_name = payload.zone_name
            zone_type = payload.zone_type
            is_revenue_zone = _yes_no_to_bool(payload.is_revenue_zone)
            hotspot_x = payload.zone_hotspot_x
            hotspot_y = payload.zone_hotspot_y
            is_face_hidden = None
            db_event_type = EventType(payload.event_type)
        else:
            raise ValueError(f"Unsupported event payload type: {type(payload).__name__}")

        # 2. Ensure the store/camera/zone this event references actually exist.
        # Event/TrackedEntity/IdentityAlias all carry real foreign keys to these
        # tables; SQLite does not enforce them by default so this was silently
        # tolerated before, but PostgreSQL does enforce them.
        self.reference_data.ensure_store(store_id)
        self.reference_data.ensure_camera(store_id, payload.camera_id)
        if zone_id:
            self.reference_data.ensure_zone(
                store_id, zone_id, name=zone_name, zone_type=zone_type, is_revenue_zone=is_revenue_zone
            )

        # 3. Idempotency: (store_id, source_event_id) identifies a logical event,
        # not source_event_id alone -- two different stores may reuse the same
        # source-provided event id. A replay of the same logical event returns the
        # already-persisted row without creating a duplicate; the raw payload is
        # still recorded (see _record_raw_event) so the replay itself is auditable.
        #
        # The CCTV pipeline families (Entry/Exit/Reentry/Zone*/Queue*) never carry
        # an explicit event_id -- only CanonicalEvent does. For those, synthesize
        # a deterministic key from the fields that already identify "the same
        # logical occurrence" (queue_event_id for queue events; source identity +
        # camera + timestamp otherwise), so a replay is still recognized as a
        # duplicate instead of silently fanning out into a new Event every time.
        source_event_id = getattr(payload, "event_id", None)
        if not source_event_id:
            if isinstance(payload, (QueueCompletedEvent, QueueAbandonedEvent)):
                source_event_id = f"queue:{payload.queue_event_id}"
            else:
                source_event_id = (
                    f"{db_event_type.value}:{payload.camera_id}:{source_field}:"
                    f"{raw_identity_value}:{timestamp.isoformat()}"
                )

        existing_event = self.db.execute(
            select(Event).where(
                Event.store_id == store_id,
                Event.source_event_id == source_event_id,
            )
        ).scalar_one_or_none()
        if existing_event is not None:
            logger.info("duplicate_event_ignored", event_id=source_event_id, store_id=store_id)
            self._record_raw_event(
                payload,
                store_id=store_id,
                event_id=existing_event.id,
                validation_status="duplicate",
                replay_job_id=replay_job_id,
            )
            return existing_event

        # 4. Resolve (not reuse-as-primary-key) the canonical visitor identity.
        entity = self._resolve_tracked_entity(
            store_id=store_id,
            camera_id=payload.camera_id,
            source_field=source_field,
            source_value=raw_identity_value,
            camera_scoped=identity_camera_scoped,
            timestamp=timestamp,
            gender=gender,
            age=age,
            age_bucket=age_bucket,
            is_staff=is_staff,
            group_id=group_id,
            group_size=group_size,
        )
        person_id = entity.id

        # 5. Get or Create VisitSession
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

        # 6. Append Event Record
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
                EventType.QUEUE_COMPLETED,
                EventType.QUEUE_ABANDONED,
                EventType.BILLING_QUEUE_JOIN,
            } else payload.metadata.get("wait_seconds")
            queue_position_at_join = payload.metadata.get("queue_depth")
            abandoned = db_event_type == EventType.QUEUE_ABANDONED

        event_id_value = str(uuid.uuid4())
        new_event = Event(
            id=event_id_value,
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
            is_replay=replay_job_id is not None,
            replay_job_id=replay_job_id,
        )
        self.db.add(new_event)
        self._record_raw_event(
            payload,
            store_id=store_id,
            event_id=event_id_value,
            validation_status="accepted",
            replay_job_id=replay_job_id,
        )

        # 7. Close Session on Exit
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
        if run_inference:
            VisitorInferenceService(self.db).infer_store(store_id)
        return new_event

    def _resolve_tracked_entity(
        self,
        *,
        store_id: str,
        camera_id: str,
        source_field: str,
        source_value: str,
        camera_scoped: bool,
        timestamp: datetime,
        gender: str | None,
        age: int | None,
        age_bucket: str | None,
        is_staff: bool,
        group_id: str | None,
        group_size: int | None,
    ) -> TrackedEntity:
        """Resolve a raw source identifier to a canonical TrackedEntity via
        IdentityAlias, creating both the first time this (store, camera scope,
        source field, source value) combination is seen.

        This performs no cross-alias matching or merging -- each distinct raw
        identity in its namespace maps to exactly one TrackedEntity,
        deterministically, the same behavior as the prior composite-string id
        scheme. What changed is the storage model (a real alias table with a
        canonical UUID id, instead of encoding the store into the primary key
        string), which is what makes future identity-linking work possible
        without another identity-model migration.
        """
        alias_camera_scope = camera_id if camera_scoped else STORE_SCOPED_CAMERA_ID

        alias = self.db.execute(
            select(IdentityAlias).where(
                IdentityAlias.store_id == store_id,
                IdentityAlias.camera_id == alias_camera_scope,
                IdentityAlias.source_field == source_field,
                IdentityAlias.source_value == source_value,
            )
        ).scalar_one_or_none()

        if alias is not None:
            alias.last_seen_at = timestamp
            entity = self.db.get(TrackedEntity, alias.tracked_entity_id)
            if is_staff and entity is not None and not entity.is_staff:
                entity.is_staff = True
                entity.staff_confidence_score = 1.0
                entity.staff_inference_reason = "source marked as staff"
            return entity

        entity = TrackedEntity(
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
        self.db.flush()  # populate entity.id (UUID default) for the alias FK

        self.db.add(
            IdentityAlias(
                tracked_entity_id=entity.id,
                store_id=store_id,
                camera_id=alias_camera_scope,
                source_field=source_field,
                source_value=source_value,
                confidence=1.0,
                first_seen_at=timestamp,
                last_seen_at=timestamp,
            )
        )
        self.db.flush()
        return entity

    def _record_raw_event(
        self,
        payload: EventPayload,
        *,
        store_id: str,
        event_id: str | None,
        validation_status: str,
        replay_job_id: str | None = None,
    ) -> None:
        self.db.add(
            RawEvent(
                source=type(payload).__name__,
                source_event_id=getattr(payload, "event_id", None),
                store_id=store_id,
                payload_json=payload.model_dump_json(),
                event_id=event_id,
                validation_status=validation_status,
                replay_job_id=replay_job_id,
            )
        )

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
            CanonicalEventType.BILLING_QUEUE_COMPLETE: EventType.QUEUE_COMPLETED,
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


def event_payload_store_and_timestamp(payload: EventPayload) -> tuple[str, datetime]:
    """Return ``(store_id, timestamp)`` for any ``EventPayload`` variant,
    without running the rest of ``process_event``'s normalization.

    Used by ``ReplayService`` to sort/filter raw payloads by their own
    historical timestamp *before* touching the database -- see
    ``process_event``'s step 1 for the (more complete) per-family field
    mapping this mirrors just the two fields of.
    """
    if isinstance(payload, CanonicalEvent):
        return payload.store_id, payload.timestamp
    if isinstance(payload, (EntryEvent, ExitEvent, ReentryEvent)):
        return payload.store_code, payload.event_timestamp
    if isinstance(payload, (ZoneEnteredEvent, ZoneExitedEvent)):
        return payload.store_id, payload.event_time
    if isinstance(payload, (QueueCompletedEvent, QueueAbandonedEvent)):
        return payload.store_id, payload.queue_exit_ts
    raise ValueError(f"Unsupported event payload type: {type(payload).__name__}")


def _parse_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def _yes_no_to_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value == "Yes"
