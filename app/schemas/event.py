# app/schemas/event.py
# ----------------------------------------------------------------------
# Pydantic schemas used by the Event ingestion endpoint.
# ----------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing_extensions import Annotated

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
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class BaseEvent(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)
    event_id: Optional[str] = None
    confidence: Optional[float] = None
    metadata: Optional[dict[str, Any]] = None

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
    is_staff: Optional[bool] = False
    gender_pred: Optional[str] = None
    age_pred: Optional[int] = None
    age_bucket: Optional[str] = None
    is_face_hidden: Optional[bool] = False
    group_id: Optional[str] = None
    group_size: Optional[int] = None

class ExitEvent(BaseEvent):
    event_type: Literal[EventType.EXIT] = Field(default=EventType.EXIT)
    id_token: str
    store_code: str
    camera_id: str
    event_timestamp: datetime
    is_staff: Optional[bool] = False
    gender_pred: Optional[str] = None
    age_pred: Optional[int] = None
    age_bucket: Optional[str] = None
    is_face_hidden: Optional[bool] = False
    group_id: Optional[str] = None
    group_size: Optional[int] = None

class ZoneEventBase(BaseEvent):
    track_id: str
    store_id: str
    camera_id: str
    zone_id: str
    zone_name: Optional[str] = None
    zone_type: Optional[str] = None
    is_revenue_zone: Optional[str] = None
    zone_hotspot_x: Optional[float] = None
    zone_hotspot_y: Optional[float] = None
    gender: Optional[str] = None
    age: Optional[int] = None
    age_bucket: Optional[str] = None

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
    zone_type: Optional[str] = None
    queue_join_ts: datetime
    queue_served_ts: Optional[datetime] = None
    queue_exit_ts: datetime
    wait_seconds: Optional[int] = None
    queue_position_at_join: Optional[int] = None
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
        CanonicalEventType.BILLING_QUEUE_ABANDON,
        CanonicalEventType.REENTRY,
    ]
    timestamp: datetime
    zone_id: Optional[str] = None
    dwell_ms: Optional[int] = 0
    is_staff: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

# ----------------------------------------------------------------------
# 3️⃣ Union with discriminator – FastAPI will automatically pick the right
#    model based on the "event_type" field.
# ----------------------------------------------------------------------
EventPayload = Annotated[
    Union[
        EntryEvent,
        ExitEvent,
        ReentryEvent,
        ZoneEnteredEvent,
        ZoneExitedEvent,
        QueueCompletedEvent,
        QueueAbandonedEvent,
        CanonicalEvent,
    ],
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
