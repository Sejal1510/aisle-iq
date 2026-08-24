# app/schemas/event.py
# ----------------------------------------------------------------------
# Pydantic schemas used by the Event ingestion endpoint.
# ----------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ----------------------------------------------------------------------
# 1️⃣ Event type discriminators (match the JSON “event_type” field)
# ----------------------------------------------------------------------
class EventType(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"
    ZONE_ENTERED = "zone_entered"
    ZONE_EXITED = "zone_exited"
    ZONE_DWELL = "zone_dwell"
    BILLING_QUEUE_JOIN = "billing_queue_join"
    QUEUE_COMPLETED = "queue_completed"
    QUEUE_ABANDONED = "queue_abandoned"
    REENTRY = "reentry"


class CanonicalEventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_COMPLETE = "BILLING_QUEUE_COMPLETE"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class BaseEvent(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)
    event_id: str | None = None
    confidence: float | None = None
    metadata: dict[str, Any] | None = None

# ----------------------------------------------------------------------
# 2️⃣ Concrete request bodies – one model per possible payload.
# ----------------------------------------------------------------------
class EntryEvent(BaseEvent):
    event_type: Literal[EventType.ENTRY] = Field(default=EventType.ENTRY)
    id_token: str
    store_code: str
    camera_id: str
    event_timestamp: datetime
    # optional demographic fields
    is_staff: bool | None = False
    gender_pred: str | None = None
    age_pred: int | None = None
    age_bucket: str | None = None
    is_face_hidden: bool | None = False
    group_id: str | None = None
    group_size: int | None = None

class ExitEvent(BaseEvent):
    event_type: Literal[EventType.EXIT] = Field(default=EventType.EXIT)
    id_token: str
    store_code: str
    camera_id: str
    event_timestamp: datetime
    is_staff: bool | None = False
    gender_pred: str | None = None
    age_pred: int | None = None
    age_bucket: str | None = None
    is_face_hidden: bool | None = False
    group_id: str | None = None
    group_size: int | None = None

class ZoneEventBase(BaseEvent):
    track_id: str
    store_id: str
    camera_id: str
    zone_id: str
    zone_name: str | None = None
    zone_type: str | None = None
    is_revenue_zone: str | None = None
    zone_hotspot_x: float | None = None
    zone_hotspot_y: float | None = None
    gender: str | None = None
    age: int | None = None
    age_bucket: str | None = None

    @field_validator("track_id", mode="before")
    @classmethod
    def normalize_track_id(cls, value: object) -> object:
        if value is None:
            return value
        return str(value)


class ZoneEnteredEvent(ZoneEventBase):
    event_type: Literal[EventType.ZONE_ENTERED] = Field(default=EventType.ZONE_ENTERED)
    event_time: datetime


class ZoneExitedEvent(ZoneEventBase):
    event_type: Literal[EventType.ZONE_EXITED] = Field(default=EventType.ZONE_EXITED)
    event_time: datetime


class QueueEventBase(ZoneEventBase):
    queue_event_id: str
    zone_type: str | None = None
    queue_join_ts: datetime
    queue_served_ts: datetime | None = None
    queue_exit_ts: datetime
    wait_seconds: int | None = None
    queue_position_at_join: int | None = None
    abandoned: bool


class QueueCompletedEvent(QueueEventBase):
    event_type: Literal[EventType.QUEUE_COMPLETED] = Field(default=EventType.QUEUE_COMPLETED)
    abandoned: bool = False


class QueueAbandonedEvent(QueueEventBase):
    event_type: Literal[EventType.QUEUE_ABANDONED] = Field(default=EventType.QUEUE_ABANDONED)
    abandoned: bool = True


class ReentryEvent(EntryEvent):
    event_type: Literal[EventType.REENTRY] = Field(default=EventType.REENTRY)


class CanonicalEvent(BaseEvent):
    event_id: str
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: Literal[
        CanonicalEventType.ENTRY,
        CanonicalEventType.EXIT,
        CanonicalEventType.ZONE_ENTER,
        CanonicalEventType.ZONE_EXIT,
        CanonicalEventType.ZONE_DWELL,
        CanonicalEventType.BILLING_QUEUE_JOIN,
        CanonicalEventType.BILLING_QUEUE_COMPLETE,
        CanonicalEventType.BILLING_QUEUE_ABANDON,
        CanonicalEventType.REENTRY,
    ]
    timestamp: datetime
    zone_id: str | None = None
    dwell_ms: int | None = 0
    is_staff: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

# ----------------------------------------------------------------------
# 3️⃣ Union with discriminator – FastAPI will automatically pick the right
#    model based on the "event_type" field.
# ----------------------------------------------------------------------
EventPayload = Annotated[
    EntryEvent | ExitEvent | ReentryEvent | ZoneEnteredEvent | ZoneExitedEvent | QueueCompletedEvent | QueueAbandonedEvent | CanonicalEvent,
    Field(discriminator="event_type"),
]

# ----------------------------------------------------------------------
# 4️⃣ Response model (to be used by the API later)
# ----------------------------------------------------------------------
class EventIngestionResponse(BaseModel):
    """Schema for the response returned by the ingestion endpoint."""

    status: str
    event_type: str
    tracked_entity_id: str
    timestamp: datetime
