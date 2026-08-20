from __future__ import annotations

import statistics
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import EventType
from app.models.event import Event
from app.schemas.live_analytics import (
    CurrentQueueResponse,
    QueuedEntity,
    QueueMetricsResponse,
)
from app.services.occupancy_service import to_naive

_TERMINAL_TYPES = (EventType.QUEUE_COMPLETED, EventType.QUEUE_ABANDONED)


class QueueService:
    """Live and period queue intelligence, correlated by ``Event.queue_event_id``
    rather than by TrackedEntity.

    Each distinct queue_event_id is one queue visit, independent of how many
    visits the same entity has made (join -> complete -> another join is two
    separate, independently tracked visits). This is what the source data
    already gives us -- the video pipeline mints a fresh queue_event_id every
    time a track enters the queue polygon (see pipeline/video/events.py's
    QueueState), and both the canonical and legacy queue event schemas carry
    queue_event_id through to the persisted Event row at ingestion.

    Deliberately NOT "fixed" here, only defined and tested (see
    tests/test_queue_service.py):
    - A terminal event (complete/abandon) with no matching join Event row
      (legacy aggregate queue_completed/queue_abandoned records, or a
      genuinely dropped join) is still counted using its own wait_seconds --
      it simply never appeared in "currently queued" before completing,
      since there was nothing to make it "currently queued" from.
    - Two terminal Event rows referencing the same queue_event_id (e.g. a
      completed record and a later, inconsistent abandoned record for the
      same visit) both count in their own respective buckets; this service
      does not attempt to guess which one is "correct".

    One thing that IS collapsed here: current_queue represents active queue
    *visits*, and queue_event_id is the identity of a visit -- two JOIN
    Event rows sharing one queue_event_id (an anomalous duplicate/near-
    duplicate submission, not a second real visit) describe the same visit,
    not two people in line, so they are grouped to a single QueuedEntity
    using the earliest observed join timestamp. This does not affect two
    genuinely separate visits by the same entity (join A -> complete A ->
    join B): A and B are distinct queue_event_id values and remain two
    independent entries.
    """

    def __init__(self, db: Session):
        self.db = db

    def current_queue(self, store_id: str, as_of: datetime | None = None) -> CurrentQueueResponse:
        resolved_as_of = self._resolve_as_of(store_id, as_of)

        joined = self.db.execute(
            select(Event.tracked_entity_id, Event.queue_event_id, Event.timestamp)
            .where(Event.store_id == store_id)
            .where(Event.event_type == EventType.BILLING_QUEUE_JOIN)
            .where(Event.queue_event_id.is_not(None))
            .where(Event.timestamp <= resolved_as_of)
        ).all()

        terminal_queue_event_ids = set(
            self.db.scalars(
                select(Event.queue_event_id)
                .where(Event.store_id == store_id)
                .where(Event.event_type.in_(_TERMINAL_TYPES))
                .where(Event.queue_event_id.is_not(None))
                .where(Event.timestamp <= resolved_as_of)
            ).all()
        )

        # Group by queue_event_id first: a duplicate/near-duplicate JOIN
        # submission for the same visit must not appear as a second person
        # in line. Keep the earliest observed join for each visit.
        earliest_join_by_visit: dict[str, tuple[str, datetime]] = {}
        for tracked_entity_id, queue_event_id, joined_at in joined:
            existing = earliest_join_by_visit.get(queue_event_id)
            if existing is None or joined_at < existing[1]:
                earliest_join_by_visit[queue_event_id] = (tracked_entity_id, joined_at)

        queued_entities = [
            QueuedEntity(
                tracked_entity_id=tracked_entity_id,
                queue_event_id=queue_event_id,
                joined_at=joined_at,
                waiting_seconds=max(0, int((resolved_as_of - joined_at).total_seconds())),
            )
            for queue_event_id, (tracked_entity_id, joined_at) in earliest_join_by_visit.items()
            if queue_event_id not in terminal_queue_event_ids
        ]
        queued_entities.sort(key=lambda entity: entity.joined_at)

        return CurrentQueueResponse(
            store_id=store_id,
            as_of=resolved_as_of,
            queue_length=len(queued_entities),
            queued_entities=queued_entities,
        )

    def queue_metrics(self, store_id: str, start: datetime, end: datetime) -> QueueMetricsResponse:
        start = to_naive(start)
        end = to_naive(end)
        if end <= start:
            raise ValueError("end must be after start")

        terminal_rows = self.db.execute(
            select(Event.event_type, Event.wait_seconds)
            .where(Event.store_id == store_id)
            .where(Event.event_type.in_(_TERMINAL_TYPES))
            .where(Event.timestamp >= start)
            .where(Event.timestamp < end)
        ).all()

        completed = sum(1 for event_type, _ in terminal_rows if event_type == EventType.QUEUE_COMPLETED)
        abandoned = sum(1 for event_type, _ in terminal_rows if event_type == EventType.QUEUE_ABANDONED)
        wait_seconds = [wait for _, wait in terminal_rows if wait is not None]

        total = completed + abandoned
        abandonment_rate = round(abandoned / total, 4) if total else 0.0
        average_wait = round(sum(wait_seconds) / len(wait_seconds), 2) if wait_seconds else None
        median_wait = round(statistics.median(wait_seconds), 2) if wait_seconds else None

        return QueueMetricsResponse(
            store_id=store_id,
            start=start,
            end=end,
            completed_visits=completed,
            abandoned_visits=abandoned,
            abandonment_rate=abandonment_rate,
            average_wait_seconds=average_wait,
            median_wait_seconds=median_wait,
        )

    def _resolve_as_of(self, store_id: str, as_of: datetime | None) -> datetime:
        if as_of is not None:
            return to_naive(as_of)
        latest = self.db.scalar(select(func.max(Event.timestamp)).where(Event.store_id == store_id))
        if latest is not None:
            return latest
        return datetime.now(timezone.utc).replace(tzinfo=None)
