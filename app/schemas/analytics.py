from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime


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
    # Customer ENTRY events (entrance-line crossings, staff excluded) -- the
    # store-level visitor count. total_visitors above counts camera-scoped
    # visit sessions, so one person seen by three cameras is three sessions.
    footfall: int = 0
    # Resolved customer queue visits (completed + abandoned) behind
    # queue_abandonment_rate / average_queue_wait_seconds -- the sample size.
    queue_visits: int = 0
    # Whether any POS transaction exists for the store. Without POS data,
    # conversion and revenue are unknown, not zero.
    has_pos_data: bool = False


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
    # What the first step counts: "store_entries" (customer ENTRY events,
    # when the store has an entrance camera) or "tracked_sessions".
    visitor_basis: str = "tracked_sessions"


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
    current_period_start: UtcDatetime | None = None
    current_period_end: UtcDatetime | None = None
    previous_period_start: UtcDatetime | None = None
    previous_period_end: UtcDatetime | None = None
    related_signals: list[str] = Field(default_factory=list)


class StoreAnomaliesResponse(BaseModel):
    store_id: str
    anomalies: list[StoreAnomaly] = Field(default_factory=list)
