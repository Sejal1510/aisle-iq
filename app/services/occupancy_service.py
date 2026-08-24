from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.tracking import TrackedEntity, VisitSession
from app.schemas.live_analytics import (
    CurrentOccupancyResponse,
    OccupancyHistoryResponse,
    OccupancyPoint,
)


def to_naive(value: datetime) -> datetime:
    """Normalize a possibly tz-aware datetime to the naive form this system
    stores everywhere. Naive timestamps are treated as UTC throughout this
    codebase (no per-store local timezone is modeled), so an aware input is
    converted to UTC before the tzinfo is dropped, rather than dropped as-is
    (which would silently reinterpret it in the wrong offset)."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


class OccupancyService:
    """Current and historical store occupancy, derived from VisitSession.

    Occupancy counts customers only (TrackedEntity.is_staff is not True),
    consistent with AnalyticsService's existing "staff excluded from customer
    metrics by default" convention. A session is "occupying" the store over
    the half-open interval [entry_time, exit_time) -- present at the instant
    it opens, no longer present at the instant it closes.

    Edge-case behavior this relies on from ingestion (EventIngestionService),
    not re-implemented here:
    - Duplicate event replay is deduplicated at ingestion (store-scoped
      idempotency), so occupancy reflects the deduplicated state, not raw
      submission counts.
    - Repeated ENTRY without an EXIT reuses the existing IN_PROGRESS session
      rather than opening a second one -- occupancy does not double-count it.
    - EXIT without a prior ENTRY creates a zero-dwell, already-COMPLETED
      session at ingestion -- it is never IN_PROGRESS, so it never
      contributes to occupancy at any as_of point.
    - Cross-camera identity resolution (and its known limitation: no
      cross-camera ReID for camera-scoped identifiers) is entirely a P2
      concern; this service only ever queries the resulting VisitSession
      rows and does not reason about cameras itself.

    One case ingestion does NOT guarantee: VisitSession's "reuse the active
    session" lookup keys only on session_status == IN_PROGRESS, not on event
    timestamp order, so out-of-order delivery can produce two overlapping
    VisitSession rows for the same TrackedEntity (e.g. ENTRY 09:00, EXIT
    09:30, then an ENTRY timestamped 09:15 arriving after the EXIT was
    already processed -- there is no IN_PROGRESS session left to reuse, so a
    second, overlapping one is opened). That is a pre-existing ingestion
    characteristic, not something this read layer re-derives or corrects.
    What this layer does guarantee: occupancy counts DISTINCT
    TrackedEntity.id among matching sessions, not matching VisitSession rows,
    so however many overlapping session rows exist for one entity, that
    entity is never counted more than once at a given instant. See
    test_occupancy_service.py's out-of-order regression test.
    """

    def __init__(self, db: Session):
        self.db = db

    def current_occupancy(self, store_id: str, as_of: datetime | None = None) -> CurrentOccupancyResponse:
        resolved_as_of = self.resolve_as_of(store_id, as_of)
        return CurrentOccupancyResponse(
            store_id=store_id,
            as_of=resolved_as_of,
            occupancy=self.occupancy_at(store_id, resolved_as_of),
        )

    def occupancy_at(self, store_id: str, as_of: datetime) -> int:
        as_of = to_naive(as_of)
        return int(
            self.db.scalar(
                # DISTINCT tracked_entity_id, not count of VisitSession rows:
                # a canonical entity can only be in one place at a time, so
                # this is what makes overlapping session rows for the same
                # entity (see class docstring) uncountable as two people.
                select(func.count(func.distinct(VisitSession.tracked_entity_id)))
                .join(TrackedEntity, TrackedEntity.id == VisitSession.tracked_entity_id)
                .where(VisitSession.store_id == store_id)
                .where(TrackedEntity.is_staff.is_not(True))
                .where(VisitSession.entry_time <= as_of)
                .where((VisitSession.exit_time.is_(None)) | (VisitSession.exit_time > as_of))
            )
            or 0
        )

    def occupancy_series(
        self, store_id: str, start: datetime, end: datetime, bucket_minutes: int = 60
    ) -> OccupancyHistoryResponse:
        """Point-in-time occupancy samples at each bucket boundary, INCLUSIVE
        of `end` (a final sample at the end of the range is meaningful --
        "occupancy at closing time"). This differs deliberately from
        TimeSeriesService.bucket_boundaries, which is exclusive of `end`: that
        helper sums counts *within* each interval, where a trailing partial
        interval wouldn't be meaningful, whereas this takes an instantaneous
        snapshot at each point, where the boundary itself is a valid sample."""
        start = to_naive(start)
        end = to_naive(end)
        if end <= start:
            raise ValueError("end must be after start")
        if bucket_minutes <= 0:
            raise ValueError("bucket_minutes must be positive")

        bucket_delta = timedelta(minutes=bucket_minutes)

        sessions = self.db.execute(
            select(VisitSession.tracked_entity_id, VisitSession.entry_time, VisitSession.exit_time)
            .join(TrackedEntity, TrackedEntity.id == VisitSession.tracked_entity_id)
            .where(VisitSession.store_id == store_id)
            .where(TrackedEntity.is_staff.is_not(True))
            .where(VisitSession.entry_time <= end)
            .where((VisitSession.exit_time.is_(None)) | (VisitSession.exit_time >= start))
        ).all()

        points: list[OccupancyPoint] = []
        cursor = start
        while cursor <= end:
            # DISTINCT entity ids at this instant, not matching-row count --
            # see occupancy_at for why (overlapping sessions for one entity
            # must not count as two people).
            occupying_entities = {
                tracked_entity_id
                for tracked_entity_id, entry_time, exit_time in sessions
                if entry_time <= cursor and (exit_time is None or exit_time > cursor)
            }
            points.append(OccupancyPoint(bucket_start=cursor, occupancy=len(occupying_entities)))
            cursor += bucket_delta

        return OccupancyHistoryResponse(
            store_id=store_id, start=start, end=end, bucket_minutes=bucket_minutes, points=points
        )

    def average_occupancy(self, store_id: str, start: datetime, end: datetime, bucket_minutes: int = 60) -> float:
        series = self.occupancy_series(store_id, start, end, bucket_minutes=bucket_minutes)
        if not series.points:
            return 0.0
        return round(sum(point.occupancy for point in series.points) / len(series.points), 2)

    def resolve_as_of(self, store_id: str, as_of: datetime | None) -> datetime:
        """"Current" means as of the latest ingested event for this store, not
        wall-clock now(). This is an event-log-driven system fed by batch/
        offline video processing, not a live camera stream -- treating
        wall-clock time as "now" would make historical or demo data always
        read as empty or arbitrarily stale. If the store has no events yet,
        there is nothing to be "as of"; wall-clock time is used purely as a
        stable fallback since occupancy is trivially zero either way."""
        if as_of is not None:
            return to_naive(as_of)

        latest = self.db.scalar(select(func.max(Event.timestamp)).where(Event.store_id == store_id))
        if latest is not None:
            return latest
        return datetime.now(UTC).replace(tzinfo=None)
