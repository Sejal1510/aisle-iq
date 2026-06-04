from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import CorrelationStatus, EventType
from app.models.event import Event
from app.models.pos import TransactionCorrelation
from app.models.tracking import TrackedEntity, VisitSession
from app.schemas.path_analytics import PathMetric, StorePathAnalyticsResponse


@dataclass
class PathAccumulator:
    visitor_count: int = 0
    purchase_count: int = 0
    abandonment_count: int = 0


class PathAnalyticsService:
    """Derive customer journey paths from existing zone and purchase facts."""

    def __init__(self, db: Session):
        self.db = db

    def get_store_paths(self, store_id: str, *, limit: int = 5) -> StorePathAnalyticsResponse:
        session_ids = self._customer_session_ids(store_id)
        purchased_session_ids = self._purchased_session_ids(session_ids)
        abandoned_session_ids = self._queue_abandoned_session_ids(session_ids)
        paths_by_session = self._paths_by_session(session_ids)

        accumulators: dict[tuple[str, ...], PathAccumulator] = defaultdict(PathAccumulator)
        for session_id in session_ids:
            path = paths_by_session.get(session_id)
            if not path:
                continue

            accumulator = accumulators[path]
            accumulator.visitor_count += 1
            if session_id in purchased_session_ids:
                accumulator.purchase_count += 1
            if session_id in abandoned_session_ids or session_id not in purchased_session_ids:
                accumulator.abandonment_count += 1

        metrics = [
            self._metric(path, accumulator)
            for path, accumulator in accumulators.items()
            if accumulator.visitor_count > 0
        ]

        return StorePathAnalyticsResponse(
            store_id=store_id,
            total_journeys=sum(metric.visitor_count for metric in metrics),
            most_common_paths=sorted(
                metrics,
                key=lambda metric: (-metric.visitor_count, metric.path),
            )[:limit],
            top_purchase_journeys=sorted(
                [metric for metric in metrics if metric.purchase_count > 0],
                key=lambda metric: (-metric.purchase_count, -metric.conversion_rate, metric.path),
            )[:limit],
            path_drop_offs=sorted(
                [metric for metric in metrics if metric.abandonment_count > 0],
                key=lambda metric: (-metric.abandonment_count, -metric.abandonment_rate, metric.path),
            )[:limit],
        )

    def _customer_session_ids(self, store_id: str) -> list[str]:
        return list(
            self.db.scalars(
                select(VisitSession.id)
                .join(TrackedEntity)
                .where(VisitSession.store_id == store_id)
                .where(TrackedEntity.is_staff.is_not(True))
                .order_by(VisitSession.entry_time, VisitSession.id)
            )
        )

    def _purchased_session_ids(self, session_ids: list[str]) -> set[str]:
        if not session_ids:
            return set()
        return set(
            self.db.scalars(
                select(TransactionCorrelation.session_id)
                .where(TransactionCorrelation.session_id.in_(session_ids))
                .where(TransactionCorrelation.status == CorrelationStatus.MATCHED)
                .where(TransactionCorrelation.session_id.is_not(None))
            )
        )

    def _queue_abandoned_session_ids(self, session_ids: list[str]) -> set[str]:
        if not session_ids:
            return set()
        return set(
            self.db.scalars(
                select(Event.session_id)
                .where(Event.session_id.in_(session_ids))
                .where(Event.event_type == EventType.QUEUE_ABANDONED)
            )
        )

    def _paths_by_session(self, session_ids: list[str]) -> dict[str, tuple[str, ...]]:
        if not session_ids:
            return {}

        events = self.db.scalars(
            select(Event)
            .where(Event.session_id.in_(session_ids))
            .where(Event.zone_id.is_not(None))
            .where(Event.event_type.in_([EventType.ZONE_ENTERED, EventType.QUEUE_COMPLETED, EventType.QUEUE_ABANDONED]))
            .order_by(Event.session_id, Event.timestamp, Event.id)
        ).all()

        paths: dict[str, list[str]] = defaultdict(list)
        for event in events:
            if event.session_id is None or event.zone_id is None:
                continue

            step = event.zone_id
            if event.event_type in {EventType.QUEUE_COMPLETED, EventType.QUEUE_ABANDONED}:
                step = f"{event.zone_id}:queue"

            current_path = paths[event.session_id]
            if not current_path or current_path[-1] != step:
                current_path.append(step)

        return {
            session_id: tuple(path)
            for session_id, path in paths.items()
            if path
        }

    @staticmethod
    def _metric(path: tuple[str, ...], accumulator: PathAccumulator) -> PathMetric:
        visitor_count = accumulator.visitor_count
        return PathMetric(
            path=list(path),
            visitor_count=visitor_count,
            purchase_count=accumulator.purchase_count,
            conversion_rate=_rate(accumulator.purchase_count, visitor_count),
            abandonment_count=accumulator.abandonment_count,
            abandonment_rate=_rate(accumulator.abandonment_count, visitor_count),
        )


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)
