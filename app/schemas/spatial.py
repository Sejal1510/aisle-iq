from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.core.storage import SAFE_IDENTIFIER_PATTERN
from app.models.enums import ZoneType

# P7 security hardening: store_id is used as a filesystem directory name for
# onboarding uploads (see app.core.storage.save_upload), and camera_id/
# zone_id are validated the same way for defense-in-depth/consistency even
# though they don't currently reach the filesystem. Rejects path separators,
# "..", and other traversal-relevant characters at the API boundary --
# app.core.storage additionally enforces this again itself, so a caller that
# bypasses this schema layer still cannot escape the storage root.
SafeIdentifier = Annotated[str, Field(pattern=SAFE_IDENTIFIER_PATTERN, min_length=1, max_length=64)]


class PolygonGeometryIn(BaseModel):
    kind: Literal["polygon"] = "polygon"
    points: list[tuple[float, float]] = Field(min_length=3)


class LineGeometryIn(BaseModel):
    kind: Literal["line"] = "line"
    axis: Literal["x", "y"]
    position: float
    inside_greater_than_position: bool = True


GeometryIn = Annotated[PolygonGeometryIn | LineGeometryIn, Field(discriminator="kind")]


class GeometryOut(BaseModel):
    kind: str
    points: list[tuple[float, float]] | None = None
    axis: str | None = None
    position: float | None = None
    inside_greater_than_position: bool | None = None


class CreateStoreRequest(BaseModel):
    store_id: SafeIdentifier
    name: str | None = None


class StoreOut(BaseModel):
    store_id: str
    name: str | None = None


class MapOut(BaseModel):
    map_id: str
    store_id: str
    name: str | None
    content_type: str | None
    width_px: int | None
    height_px: int | None
    is_active: bool
    created_at: datetime
    file_url: str


class ZoneCreateRequest(BaseModel):
    zone_id: SafeIdentifier
    name: str
    zone_type: ZoneType
    is_revenue_zone: bool = False
    map_polygon: list[tuple[float, float]] | None = Field(default=None, min_length=3)


class ZoneUpdateRequest(BaseModel):
    name: str | None = None
    zone_type: ZoneType | None = None
    is_revenue_zone: bool | None = None
    map_polygon: list[tuple[float, float]] | None = Field(default=None, min_length=3)


class ZoneOut(BaseModel):
    zone_id: str
    store_id: str
    name: str
    zone_type: ZoneType
    is_revenue_zone: bool
    map_polygon: list[tuple[float, float]] | None = None


class CameraCreateRequest(BaseModel):
    camera_id: SafeIdentifier
    name: str | None = None
    role: Literal["entry", "zone", "billing"]
    video_path: str | None = None
    start_time: datetime | None = None
    sample_fps: float | None = None
    confidence_threshold: float | None = None
    queue_completion_seconds: int | None = None
    queue_abandonment_seconds: int | None = None


class CameraUpdateRequest(BaseModel):
    name: str | None = None
    role: Literal["entry", "zone", "billing"] | None = None
    video_path: str | None = None
    start_time: datetime | None = None
    sample_fps: float | None = None
    confidence_threshold: float | None = None
    queue_completion_seconds: int | None = None
    queue_abandonment_seconds: int | None = None


class CameraOut(BaseModel):
    camera_id: str
    store_id: str
    name: str | None
    role: str | None
    video_path: str | None
    reference_image_path: str | None
    reference_image_url: str | None
    start_time: datetime | None
    sample_fps: float | None
    confidence_threshold: float | None
    queue_completion_seconds: int | None
    queue_abandonment_seconds: int | None


class CoverageCreateRequest(BaseModel):
    zone_id: str | None = None
    geometry: GeometryIn


class CoverageOut(BaseModel):
    coverage_id: str
    camera_id: str
    zone_id: str | None
    geometry: GeometryOut
