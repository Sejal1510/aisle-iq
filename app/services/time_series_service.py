from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import EventType
from app.models.event import Event
from app.models.tracking import TrackedEntity
from app.schemas.live_analytics import (
    FootfallBucket,
    HourlyFootfallResponse,
    HourlyQueueActivityResponse,
    QueueActivityBucket,
)
from app.services.occupancy_service import to_naive


def bucket_boundaries(start: datetime, end: datetime, bucket_minutes: int) -> list[datetime]:
    """Shared bucketing helper: [start, start+bucket, ..., last boundary < end].
    Bucketing is done in Python (not SQL date-trunc) to stay dialect-neutral --
    this system's queries currently only run against SQLite, and P2 explicitly
    left PostgreSQL unverified; on-read aggregation is deliberately kept
    simple rather than introducing per-dialect SQL."""
    if end <= start:
        raise ValueError("end must be after start")
    if bucket_minutes <= 0:
        raise ValueError("bucket_minutes must be positive")

    delta = timedelta(minutes=bucket_minutes)
    boundaries = []
    cursor = start
    while cursor < end:
        boundaries.append(cursor)
        cursor += delta
    return boundaries


def _bucket_index(timestamp: datetime, start: datetime, bucket_minutes: int) -> int:
    elapsed_minutes = (timestamp - start).total_seconds() / 60
    return int(elapsed_minutes // bucket_minutes)


class TimeSeriesService:
    """Hourly (or arbitrary-granularity) footfall and queue-activity series
    over a caller-supplied, arbitrary [start, end) range.

    Footfall counts customers only (TrackedEntity.is_staff is not True),
    consistent with the rest of the analytics layer.
    """

    def __init__(self, db: Session):
        self.db = db

    def hourly_footfall(
        self, store_id: str, start: datetime, end: datetime, bucket_minutes: int = 60
    ) -> HourlyFootfallResponse:
        start = to_naive(start)
        end = to_naive(end)
        boundaries = bucket_boundaries(start, end, bucket_minutes)

        timestamps = self.db.scalars(
            select(Event.timestamp)
            .join(TrackedEntity, TrackedEntity.id == Event.tracked_entity_id)
            .where(Event.store_id == store_id)
            .where(Event.event_type == EventType.ENTRY)
            .where(TrackedEntity.is_staff.is_not(True))
            .where(Event.timestamp >= start)
            .where(Event.timestamp < end)
        ).all()

        counts: dict[int, int] = defaultdict(int)
        for timestamp in timestamps:
            counts[_bucket_index(timestamp, start, bucket_minutes)] += 1

        buckets = [
            FootfallBucket(bucket_start=boundary, entries=counts.get(index, 0))
            for index, boundary in enumerate(boundaries)
        ]
        return HourlyFootfallResponse(store_id=store_id, start=start, end=end, buckets=buckets)

    def hourly_queue_activity(
        self, store_id: str, start: datetime, end: datetime, bucket_minutes: int = 60
    ) -> HourlyQueueActivityResponse:
        start = to_naive(start)
        end = to_naive(end)
        boundaries = bucket_boundaries(start, end, bucket_minutes)

        rows = self.db.execute(
            select(Event.event_type, Event.timestamp)
            .where(Event.store_id == store_id)
            .where(
                Event.event_type.in_(
                    [EventType.BILLING_QUEUE_JOIN, EventType.QUEUE_COMPLETED, EventType.QUEUE_ABANDONED]
                )
            )
            .where(Event.timestamp >= start)
            .where(Event.timestamp < end)
        ).all()

        joined: dict[int, int] = defaultdict(int)
        completed: dict[int, int] = defaultdict(int)
        abandoned: dict[int, int] = defaultdict(int)
        for event_type, timestamp in rows:
            index = _bucket_index(timestamp, start, bucket_minutes)
            if event_type == EventType.BILLING_QUEUE_JOIN:
                joined[index] += 1
            elif event_type == EventType.QUEUE_COMPLETED:
                completed[index] += 1
            else:
                abandoned[index] += 1

        buckets = [
            QueueActivityBucket(
                bucket_start=boundary,
                joined=joined.get(index, 0),
                completed=completed.get(index, 0),
                abandoned=abandoned.get(index, 0),
            )
            for index, boundary in enumerate(boundaries)
        ]
        return HourlyQueueActivityResponse(store_id=store_id, start=start, end=end, buckets=buckets)
