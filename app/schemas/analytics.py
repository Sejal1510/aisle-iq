from __future__ import annotations

from datetime import datetime

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
    """(store_id, anomaly_type) is not unique -- e.g. dead_zone emits one row
    per zone. The P4.3 trend fields below are optional and default to
    None/empty specifically so the pre-existing static rules (_queue_spike,
    _conversion_drop, _dead_zone) never need to change how they construct
    this model -- their output is byte-identical to before P4.3."""

    anomaly_type: str
    severity: str
    message: str
    suggested_action: str
    metric_value: float | int | None = None

    # P4.3: populated only for trend-based (comparison-driven) anomalies --
    # i.e. only when the caller supplied start/end to the anomalies endpoint.
    # `related_signals` holds evidence-aware, non-causal observations about
    # other metrics that moved alongside the primary one (see
    # AnomalyService's clustering) -- never a claim that one caused the other.
    metric: str | None = None
    current_value: float | None = None
    comparison_value: float | None = None
    absolute_change: float | None = None
    percent_change: float | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    previous_period_start: datetime | None = None
    previous_period_end: datetime | None = None
    related_signals: list[str] = Field(default_factory=list)


class StoreAnomaliesResponse(BaseModel):
    store_id: str
    anomalies: list[StoreAnomaly] = Field(default_factory=list)
