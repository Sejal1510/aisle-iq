from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Occupancy (P3.1)
# ---------------------------------------------------------------------------


class CurrentOccupancyResponse(BaseModel):
    store_id: str
    as_of: datetime
    occupancy: int


class OccupancyPoint(BaseModel):
    bucket_start: datetime
    occupancy: int


class OccupancyHistoryResponse(BaseModel):
    store_id: str
    start: datetime
    end: datetime
    bucket_minutes: int
    points: list[OccupancyPoint] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Queue intelligence (P3.2)
# ---------------------------------------------------------------------------


class QueuedEntity(BaseModel):
    tracked_entity_id: str
    queue_event_id: str
    joined_at: datetime
    waiting_seconds: int


class CurrentQueueResponse(BaseModel):
    store_id: str
    as_of: datetime
    queue_length: int
    queued_entities: list[QueuedEntity] = Field(default_factory=list)


class QueueMetricsResponse(BaseModel):
    store_id: str
    start: datetime
    end: datetime
    completed_visits: int
    abandoned_visits: int
    abandonment_rate: float
    average_wait_seconds: float | None = None
    median_wait_seconds: float | None = None


# ---------------------------------------------------------------------------
# Time-based analytics (P3.3)
# ---------------------------------------------------------------------------


class FootfallBucket(BaseModel):
    bucket_start: datetime
    entries: int


class HourlyFootfallResponse(BaseModel):
    store_id: str
    start: datetime
    end: datetime
    buckets: list[FootfallBucket] = Field(default_factory=list)


class QueueActivityBucket(BaseModel):
    bucket_start: datetime
    joined: int
    completed: int
    abandoned: int


class HourlyQueueActivityResponse(BaseModel):
    store_id: str
    start: datetime
    end: datetime
    buckets: list[QueueActivityBucket] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Peak-hour intelligence (P3.4)
# ---------------------------------------------------------------------------


class PeakHourBucket(BaseModel):
    bucket_start: datetime
    entries: int
    rank: int


class PeakHoursResponse(BaseModel):
    store_id: str
    start: datetime
    end: datetime
    peak_hour: PeakHourBucket | None = None
    ranked_hours: list[PeakHourBucket] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Period comparison (P3.5)
# ---------------------------------------------------------------------------


class PeriodMetrics(BaseModel):
    start: datetime
    end: datetime
    footfall: int
    unique_visitors: int
    queue_joined: int
    queue_completed: int
    queue_abandoned: int
    queue_abandonment_rate: float
    average_queue_wait_seconds: float | None = None
    average_occupancy: float


class MetricDelta(BaseModel):
    absolute: float
    percent: float | None = None


class PeriodComparisonResponse(BaseModel):
    store_id: str
    current: PeriodMetrics
    previous: PeriodMetrics
    deltas: dict[str, MetricDelta] = Field(default_factory=dict)
