from __future__ import annotations

from pydantic import BaseModel, Field


class ZoneDwellMetric(BaseModel):
    zone_id: str
    visits: int
    average_dwell_seconds: float
    total_dwell_seconds: int


class StoreMetricsResponse(BaseModel):
    store_id: str
    total_visitors: int
    unique_visitors: int
    staff_visitors: int = 0
    solo_visitors: int = 0
    groups: int = 0
    average_group_size: float = 0.0
    conversion_rate: float
    average_dwell_seconds: float
    queue_abandonment_rate: float
    average_queue_wait_seconds: float
    attributed_revenue: float
    attributed_transactions: int
    zone_dwell_metrics: list[ZoneDwellMetric] = Field(default_factory=list)


class FunnelStep(BaseModel):
    step: str
    count: int
    rate_from_previous: float | None = None
    rate_from_visitors: float | None = None


class StoreFunnelResponse(BaseModel):
    store_id: str
    visitors: int
    queue_join: int
    queue_complete: int
    purchase: int
    steps: list[FunnelStep]


class HeatmapPoint(BaseModel):
    x: float
    y: float
    intensity: float
    count: int


class StoreHeatmapResponse(BaseModel):
    store_id: str
    layout_image_url: str | None = None
    grid_size: int
    total_events: int
    point_count: int
    max_count: int
    data_confidence: str = "LOW"
    points: list[HeatmapPoint] = Field(default_factory=list)


class StoreAnomaly(BaseModel):
    anomaly_type: str
    severity: str
    message: str
    suggested_action: str
    metric_value: float | int | None = None


class StoreAnomaliesResponse(BaseModel):
    store_id: str
    anomalies: list[StoreAnomaly] = Field(default_factory=list)
