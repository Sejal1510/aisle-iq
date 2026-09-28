from __future__ import annotations

import statistics
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import EventType
from app.models.event import Event
from app.models.video_processing import VideoProcessingJob, VideoProcessingStatus
from app.schemas.live_analytics import (
    CurrentQueueResponse,
    QueuedEntity,
    QueueMetricsResponse,
)
from app.services.occupancy_service import to_naive

_TERMINAL_TYPES = (EventType.QUEUE_COMPLETED, EventType.QUEUE_ABANDONED)

# P9 audit fix: a BILLING_QUEUE_JOIN with no terminal event yet is exactly
# what "currently queued" is supposed to mean for a live camera feed -- but
# pipeline.video.events._process_billing_snapshot also leaves a JOIN
# permanently without a terminal event whenever the matching exit's dwell
# falls below queue_abandonment_seconds ("boundary noise", by explicit
# design -- see test_billing_camera_short_boundary_dwell_emits_join_but_no_
# terminal_event). That is a correct, intentional choice for the CV/event-
# generation layer, but it means an unresolved JOIN is not, on its own,
# proof that someone is still standing in line: once the VideoProcessingJob
# that produced it has reached a terminal status, there are no more frames
# left to ever resolve it, and treating it as "currently queued" would be
# reporting a fact about right now from a video that finished processing
# minutes, hours, or days ago. A JOIN with no ``video_processing_job_id``
# (live/API/replay ingestion, or the offline JSONL importer) carries no such
# caveat and is left untouched.
_TERMINAL_JOB_STATUSES = (
    VideoProcessingStatus.COMPLETED,
    VideoProcessingStatus.FAILED,
    VideoProcessingStatus.PARTIAL,
)


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

    current_queue additionally excludes a JOIN whose originating
    VideoProcessingJob has already reached a terminal status (COMPLETED,
    FAILED, PARTIAL) -- see _TERMINAL_JOB_STATUSES above for why. A JOIN
    from a still-RUNNING job, or with no video_processing_job_id at all
    (live ingestion), is unaffected. queue_metrics is untouched: it only
    ever counts terminal (complete/abandon) Event rows, which are already
    resolved facts regardless of the job's current status.
    """

    def __init__(self, db: Session):
        self.db = db

    def current_queue(self, store_id: str, as_of: datetime | None = None) -> CurrentQueueResponse:
        resolved_as_of = self._resolve_as_of(store_id, as_of)

        joined = self.db.execute(
            select(
                Event.tracked_entity_id,
                Event.queue_event_id,
                Event.timestamp,
                Event.video_processing_job_id,
            )
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
        earliest_join_by_visit: dict[str, tuple[str, datetime, str | None]] = {}
        for tracked_entity_id, queue_event_id, joined_at, job_id in joined:
            existing = earliest_join_by_visit.get(queue_event_id)
            if existing is None or joined_at < existing[1]:
                earliest_join_by_visit[queue_event_id] = (tracked_entity_id, joined_at, job_id)

        # Terminal-job filter: only look up jobs actually referenced by an
        # otherwise-still-open JOIN, and only exclude the ones that are
        # themselves done -- a JOIN tied to a still-RUNNING job stays live.
        candidate_job_ids = {
            job_id
            for queue_event_id, (_, _, job_id) in earliest_join_by_visit.items()
            if job_id is not None and queue_event_id not in terminal_queue_event_ids
        }
        terminal_job_ids: set[str] = set()
        if candidate_job_ids:
            terminal_job_ids = set(
                self.db.scalars(
                    select(VideoProcessingJob.id)
                    .where(VideoProcessingJob.id.in_(candidate_job_ids))
                    .where(VideoProcessingJob.status.in_(_TERMINAL_JOB_STATUSES))
                ).all()
            )

        queued_entities = [
            QueuedEntity(
                tracked_entity_id=tracked_entity_id,
                queue_event_id=queue_event_id,
                joined_at=joined_at,
                waiting_seconds=max(0, int((resolved_as_of - joined_at).total_seconds())),
            )
            for queue_event_id, (tracked_entity_id, joined_at, job_id) in earliest_join_by_visit.items()
            if queue_event_id not in terminal_queue_event_ids and job_id not in terminal_job_ids
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
        return datetime.now(UTC).replace(tzinfo=None)
