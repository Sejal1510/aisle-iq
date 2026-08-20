from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import EventType
from app.models.event import Event
from app.models.tracking import TrackedEntity
from app.schemas.live_analytics import (
    MetricDelta,
    PeriodComparisonResponse,
    PeriodMetrics,
)
from app.services.occupancy_service import OccupancyService, to_naive
from app.services.queue_service import QueueService

_COMPARABLE_FIELDS = (
    "footfall",
    "unique_visitors",
    "queue_joined",
    "queue_completed",
    "queue_abandoned",
    "queue_abandonment_rate",
    "average_queue_wait_seconds",
    "average_occupancy",
)


class ComparisonService:
    """Equal-duration current-vs-previous-period comparison, composed from the
    other P3 services rather than re-deriving their metrics. The previous
    period is always the same duration as the current one, ending exactly
    where the current period starts -- i.e. the immediately preceding period,
    not a fixed "last week"/"last month" -- so this works for any caller-
    supplied window."""

    def __init__(self, db: Session):
        self.db = db
        self.occupancy = OccupancyService(db)
        self.queue = QueueService(db)

    def compare(self, store_id: str, current_start: datetime, current_end: datetime) -> PeriodComparisonResponse:
        current_start = to_naive(current_start)
        current_end = to_naive(current_end)
        if current_end <= current_start:
            raise ValueError("end must be after start")

        duration = current_end - current_start
        previous_start = current_start - duration
        previous_end = current_start

        current = self._period_metrics(store_id, current_start, current_end)
        previous = self._period_metrics(store_id, previous_start, previous_end)

        deltas = {
            field: self._delta(getattr(previous, field), getattr(current, field))
            for field in _COMPARABLE_FIELDS
            if getattr(previous, field) is not None and getattr(current, field) is not None
        }

        return PeriodComparisonResponse(store_id=store_id, current=current, previous=previous, deltas=deltas)

    def _period_metrics(self, store_id: str, start: datetime, end: datetime) -> PeriodMetrics:
        footfall = self._footfall_count(store_id, start, end)
        unique_visitors = self._unique_visitor_count(store_id, start, end)
        joined = self._queue_event_count(store_id, start, end, EventType.BILLING_QUEUE_JOIN)
        queue_metrics = self.queue.queue_metrics(store_id, start, end)
        average_occupancy = self.occupancy.average_occupancy(store_id, start, end)

        return PeriodMetrics(
            start=start,
            end=end,
            footfall=footfall,
            unique_visitors=unique_visitors,
            queue_joined=joined,
            queue_completed=queue_metrics.completed_visits,
            queue_abandoned=queue_metrics.abandoned_visits,
            queue_abandonment_rate=queue_metrics.abandonment_rate,
            average_queue_wait_seconds=queue_metrics.average_wait_seconds,
            average_occupancy=average_occupancy,
        )

    def _footfall_count(self, store_id: str, start: datetime, end: datetime) -> int:
        return int(
            self.db.scalar(
                select(func.count(Event.id))
                .join(TrackedEntity, TrackedEntity.id == Event.tracked_entity_id)
                .where(Event.store_id == store_id)
                .where(Event.event_type == EventType.ENTRY)
                .where(TrackedEntity.is_staff.is_not(True))
                .where(Event.timestamp >= start)
                .where(Event.timestamp < end)
            )
            or 0
        )

    def _unique_visitor_count(self, store_id: str, start: datetime, end: datetime) -> int:
        return int(
            self.db.scalar(
                select(func.count(func.distinct(Event.tracked_entity_id)))
                .join(TrackedEntity, TrackedEntity.id == Event.tracked_entity_id)
                .where(Event.store_id == store_id)
                .where(Event.event_type == EventType.ENTRY)
                .where(TrackedEntity.is_staff.is_not(True))
                .where(Event.timestamp >= start)
                .where(Event.timestamp < end)
            )
            or 0
        )

    def _queue_event_count(self, store_id: str, start: datetime, end: datetime, event_type: EventType) -> int:
        return int(
            self.db.scalar(
                select(func.count(Event.id))
                .where(Event.store_id == store_id)
                .where(Event.event_type == event_type)
                .where(Event.timestamp >= start)
                .where(Event.timestamp < end)
            )
            or 0
        )

    @staticmethod
    def _delta(previous: float, current: float) -> MetricDelta:
        absolute = round(current - previous, 4)
        # Percent change from a zero baseline is mathematically undefined
        # (not 0%, not infinite) -- left as None rather than fabricated,
        # matching PeriodComparisonResponse's contract that consumers render
        # "N/A" for a missing percent.
        percent = round((current - previous) / previous, 4) if previous else None
        return MetricDelta(absolute=absolute, percent=percent)
