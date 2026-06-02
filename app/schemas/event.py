# app/schemas/event.py
# ----------------------------------------------------------------------
# Pydantic schemas used by the Event ingestion endpoint.
# ----------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field
from typing_extensions import Annotated

# ----------------------------------------------------------------------
# 1️⃣ Event type discriminators (match the JSON “event_type” field)
# ----------------------------------------------------------------------
class EventType(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"
    ZONE_ENTERED = "zone_entered"
    ZONE_EXITED = "zone_exited"

# ----------------------------------------------------------------------
# 2️⃣ Concrete request bodies – one model per possible payload.
# ----------------------------------------------------------------------
class EntryEvent(BaseModel):
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

class ExitEvent(BaseModel):
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

class ZoneEnteredEvent(BaseModel):
    event_type: Literal[EventType.ZONE_ENTERED] = Field(default=EventType.ZONE_ENTERED)
    track_id: str
    store_id: str
    camera_id: str
    zone_id: str
    zone_name: Optional[str] = None
    zone_type: Optional[str] = None
    is_revenue_zone: Optional[str] = None
    event_time: datetime
    zone_hotspot_x: Optional[float] = None
    zone_hotspot_y: Optional[float] = None
    gender: Optional[str] = None
    age: Optional[int] = None
    age_bucket: Optional[str] = None

class ZoneExitedEvent(BaseModel):
    event_type: Literal[EventType.ZONE_EXITED] = Field(default=EventType.ZONE_EXITED)
    track_id: str
    store_id: str
    camera_id: str
    zone_id: str
    zone_name: Optional[str] = None
    zone_type: Optional[str] = None
    is_revenue_zone: Optional[str] = None
    event_time: datetime
    zone_hotspot_x: Optional[float] = None
    zone_hotspot_y: Optional[float] = None
    gender: Optional[str] = None
    age: Optional[int] = None
    age_bucket: Optional[str] = None

# ----------------------------------------------------------------------
# 3️⃣ Union with discriminator – FastAPI will automatically pick the right
#    model based on the "event_type" field.
# ----------------------------------------------------------------------
EventPayload = Annotated[
    Union[EntryEvent, ExitEvent, ZoneEnteredEvent, ZoneExitedEvent],
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
