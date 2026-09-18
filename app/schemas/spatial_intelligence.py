from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SpatialZoneOut(BaseModel):
    """A zone's spatial configuration -- its map outline (if drawn) and which
    camera(s) currently cover it. No activity/metric data here; see
    ZoneIntensityOut for that (app/services/spatial_intelligence_service.py
    keeps these two concerns -- config vs. activity -- separate on purpose)."""

    zone_id: str
    name: str
    zone_type: str
    is_revenue_zone: bool
    map_polygon: list[tuple[float, float]] | None = None
    covering_camera_ids: list[str] = Field(default_factory=list)


class StoreSpatialConfigResponse(BaseModel):
    store_id: str
    layout_image_url: str | None = None
    zones: list[SpatialZoneOut] = Field(default_factory=list)


ZoneIntensityMetric = Literal["visits", "dwell"]


class ZoneIntensityOut(BaseModel):
    """Zone-level activity, deliberately not a per-event/per-position value --
    see docs/DESIGN.md's "Spatial Configuration (P7)" section for why this
    project does not attempt to place an individual visitor at an exact point
    on the map. ``visits``/``average_dwell_seconds``/``total_dwell_seconds``
    are always the real underlying numbers (reusing
    AnalyticsService.zone_dwell_metrics unchanged) regardless of which metric
    drove ``intensity``/``rank``, so a UI can show "what this shading means"
    rather than just an abstract color."""

    zone_id: str
    name: str
    zone_type: str
    is_revenue_zone: bool
    map_polygon: list[tuple[float, float]] | None = None
    visits: int
    average_dwell_seconds: float
    total_dwell_seconds: int
    intensity: float = Field(ge=0.0, le=1.0)
    rank: int


class StoreZoneIntensityResponse(BaseModel):
    store_id: str
    layout_image_url: str | None = None
    metric: ZoneIntensityMetric
    zones: list[ZoneIntensityOut] = Field(default_factory=list)
