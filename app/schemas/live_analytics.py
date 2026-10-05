from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime

# ---------------------------------------------------------------------------
# Occupancy (P3.1)
# ---------------------------------------------------------------------------


class CurrentOccupancyResponse(BaseModel):
    store_id: str
    as_of: UtcDatetime
    occupancy: int


class OccupancyPoint(BaseModel):
    bucket_start: UtcDatetime
    occupancy: int


class OccupancyHistoryResponse(BaseModel):
    store_id: str
    start: UtcDatetime
    end: UtcDatetime
    bucket_minutes: int
    points: list[OccupancyPoint] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Queue intelligence (P3.2)
# ---------------------------------------------------------------------------


class QueuedEntity(BaseModel):
    tracked_entity_id: str
    queue_event_id: str
    joined_at: UtcDatetime
    waiting_seconds: int


class CurrentQueueResponse(BaseModel):
    store_id: str
    as_of: UtcDatetime
    queue_length: int
    queued_entities: list[QueuedEntity] = Field(default_factory=list)


class QueueMetricsResponse(BaseModel):
    store_id: str
    start: UtcDatetime
    end: UtcDatetime
    completed_visits: int
    abandoned_visits: int
    abandonment_rate: float
    average_wait_seconds: float | None = None
    median_wait_seconds: float | None = None


# ---------------------------------------------------------------------------
# Time-based analytics (P3.3)
# ---------------------------------------------------------------------------


class FootfallBucket(BaseModel):
    bucket_start: UtcDatetime
    entries: int


class HourlyFootfallResponse(BaseModel):
    store_id: str
    start: UtcDatetime
    end: UtcDatetime
    buckets: list[FootfallBucket] = Field(default_factory=list)


class QueueActivityBucket(BaseModel):
    bucket_start: UtcDatetime
    joined: int
    completed: int
    abandoned: int


class HourlyQueueActivityResponse(BaseModel):
    store_id: str
    start: UtcDatetime
    end: UtcDatetime
    buckets: list[QueueActivityBucket] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Peak-hour intelligence (P3.4)
# ---------------------------------------------------------------------------


class PeakHourBucket(BaseModel):
    bucket_start: UtcDatetime
    entries: int
    rank: int


class PeakHoursResponse(BaseModel):
    store_id: str
    start: UtcDatetime
    end: UtcDatetime
    peak_hour: PeakHourBucket | None = None
    ranked_hours: list[PeakHourBucket] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Period comparison (P3.5)
# ---------------------------------------------------------------------------


class PeriodMetrics(BaseModel):
    start: UtcDatetime
    end: UtcDatetime
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
