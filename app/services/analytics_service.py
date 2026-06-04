from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import CorrelationStatus, EventType
from app.models.event import Event
from app.models.pos import PosTransaction, PosTransactionItem, TransactionCorrelation
from app.models.tracking import TrackedEntity, VisitSession
from app.schemas.analytics import (
    FunnelStep,
    StoreFunnelResponse,
    StoreMetricsResponse,
    ZoneDwellMetric,
)


@dataclass
class ZoneDwellAccumulator:
    visits: int = 0
    total_seconds: int = 0


class AnalyticsService:
    def __init__(self, db: Session):
        self.db = db

    def get_store_metrics(self, store_id: str) -> StoreMetricsResponse:
        total_visitors = self.total_visitors(store_id)
        unique_visitors = self.unique_visitors(store_id)
        attributed_transactions = self.attributed_transaction_count(store_id)

        return StoreMetricsResponse(
            store_id=store_id,
            total_visitors=total_visitors,
            unique_visitors=unique_visitors,
            staff_visitors=self.staff_visitors(store_id),
            solo_visitors=self.solo_visitors(store_id),
            groups=self.group_count(store_id),
            average_group_size=self.average_group_size(store_id),
            conversion_rate=self._rate(attributed_transactions, total_visitors),
            average_dwell_seconds=self.average_dwell_seconds(store_id),
            queue_abandonment_rate=self.queue_abandonment_rate(store_id),
            average_queue_wait_seconds=self.average_queue_wait_seconds(store_id),
            attributed_revenue=self.attributed_revenue(store_id),
            attributed_transactions=attributed_transactions,
            zone_dwell_metrics=self.zone_dwell_metrics(store_id),
        )

    def get_store_funnel(self, store_id: str) -> StoreFunnelResponse:
        visitors = self.total_visitors(store_id)
        queue_join = self.queue_join_count(store_id)
        queue_complete = self.queue_complete_count(store_id)
        purchase = self.attributed_transaction_count(store_id)

        steps = [
            FunnelStep(
                step="visitors",
                count=visitors,
                rate_from_previous=None,
                rate_from_visitors=1.0 if visitors else 0.0,
            ),
            FunnelStep(
                step="queue_join",
                count=queue_join,
                rate_from_previous=self._rate(queue_join, visitors),
                rate_from_visitors=self._rate(queue_join, visitors),
            ),
            FunnelStep(
                step="queue_complete",
                count=queue_complete,
                rate_from_previous=self._rate(queue_complete, queue_join),
                rate_from_visitors=self._rate(queue_complete, visitors),
            ),
            FunnelStep(
                step="purchase",
                count=purchase,
                rate_from_previous=self._rate(purchase, queue_complete),
                rate_from_visitors=self._rate(purchase, visitors),
            ),
        ]

        return StoreFunnelResponse(
            store_id=store_id,
            visitors=visitors,
            queue_join=queue_join,
            queue_complete=queue_complete,
            purchase=purchase,
            steps=steps,
        )

    def total_visitors(self, store_id: str) -> int:
        return self._scalar_int(
            select(func.count(VisitSession.id))
            .join(TrackedEntity)
            .where(VisitSession.store_id == store_id)
            .where(TrackedEntity.is_staff.is_not(True))
        )

    def unique_visitors(self, store_id: str) -> int:
        return self._scalar_int(
            select(func.count(func.distinct(VisitSession.tracked_entity_id)))
            .join(TrackedEntity)
            .where(VisitSession.store_id == store_id)
            .where(TrackedEntity.is_staff.is_not(True))
        )

    def staff_visitors(self, store_id: str) -> int:
        return self._scalar_int(
            select(func.count(func.distinct(VisitSession.tracked_entity_id)))
            .join(TrackedEntity)
            .where(VisitSession.store_id == store_id)
            .where(TrackedEntity.is_staff.is_(True))
        )

    def solo_visitors(self, store_id: str) -> int:
        return self._scalar_int(
            select(func.count(func.distinct(VisitSession.tracked_entity_id)))
            .join(TrackedEntity)
            .where(VisitSession.store_id == store_id)
            .where(TrackedEntity.is_staff.is_not(True))
            .where(TrackedEntity.group_id.is_(None))
        )

    def group_count(self, store_id: str) -> int:
        return self._scalar_int(
            select(func.count(func.distinct(TrackedEntity.group_id)))
            .where(TrackedEntity.store_id == store_id)
            .where(TrackedEntity.is_staff.is_not(True))
            .where(TrackedEntity.group_id.is_not(None))
        )

    def average_group_size(self, store_id: str) -> float:
        group_sizes = self.db.scalars(
            select(func.max(TrackedEntity.group_size))
            .where(TrackedEntity.store_id == store_id)
            .where(TrackedEntity.is_staff.is_not(True))
            .where(TrackedEntity.group_id.is_not(None))
            .group_by(TrackedEntity.group_id)
        ).all()
        if not group_sizes:
            return 0.0
        return round(sum(int(size or 0) for size in group_sizes) / len(group_sizes), 2)

    def average_dwell_seconds(self, store_id: str) -> float:
        average = self.db.scalar(
            select(func.avg(VisitSession.dwell_seconds))
            .join(TrackedEntity)
            .where(VisitSession.store_id == store_id)
            .where(TrackedEntity.is_staff.is_not(True))
            .where(VisitSession.dwell_seconds.is_not(None))
        )
        return round(float(average or 0.0), 2)

    def queue_abandonment_rate(self, store_id: str) -> float:
        abandoned = self._queue_terminal_count(store_id, EventType.QUEUE_ABANDONED)
        completed = self._queue_terminal_count(store_id, EventType.QUEUE_COMPLETED)
        return self._rate(abandoned, abandoned + completed)

    def average_queue_wait_seconds(self, store_id: str) -> float:
        average = self.db.scalar(
            select(func.avg(Event.wait_seconds))
            .where(Event.store_id == store_id)
            .where(Event.event_type.in_([EventType.QUEUE_COMPLETED, EventType.QUEUE_ABANDONED]))
            .where(Event.wait_seconds.is_not(None))
        )
        return round(float(average or 0.0), 2)

    def attributed_revenue(self, store_id: str) -> float:
        matched_transaction_ids = (
            select(TransactionCorrelation.transaction_id)
            .where(TransactionCorrelation.status == CorrelationStatus.MATCHED)
            .where(TransactionCorrelation.transaction_id.is_not(None))
            .distinct()
        )
        total = self.db.scalar(
            select(func.sum(PosTransactionItem.amount))
            .join(PosTransaction, PosTransaction.id == PosTransactionItem.transaction_id)
            .where(PosTransaction.store_id == store_id)
            .where(PosTransaction.id.in_(matched_transaction_ids))
        )
        return round(float(total or 0.0), 2)

    def attributed_transaction_count(self, store_id: str) -> int:
        return self._scalar_int(
            select(func.count(func.distinct(PosTransaction.id)))
            .join(TransactionCorrelation, TransactionCorrelation.transaction_id == PosTransaction.id)
            .where(PosTransaction.store_id == store_id)
            .where(TransactionCorrelation.status == CorrelationStatus.MATCHED)
        )

    def queue_join_count(self, store_id: str) -> int:
        return self._scalar_int(
            select(func.count(func.distinct(Event.session_id)))
            .where(Event.store_id == store_id)
            .where(Event.event_type.in_([EventType.BILLING_QUEUE_JOIN, EventType.QUEUE_COMPLETED, EventType.QUEUE_ABANDONED]))
        )

    def queue_complete_count(self, store_id: str) -> int:
        return self._scalar_int(
            select(func.count(func.distinct(Event.session_id)))
            .where(Event.store_id == store_id)
            .where(Event.event_type == EventType.QUEUE_COMPLETED)
        )

    def zone_dwell_metrics(self, store_id: str) -> list[ZoneDwellMetric]:
        events = self.db.scalars(
            select(Event)
            .where(Event.store_id == store_id)
            .where(Event.zone_id.is_not(None))
            .where(Event.event_type.in_([EventType.ZONE_ENTERED, EventType.ZONE_EXITED]))
            .order_by(Event.session_id, Event.zone_id, Event.timestamp)
        ).all()

        active_entries: dict[tuple[str, str], Event] = {}
        accumulators: dict[str, ZoneDwellAccumulator] = defaultdict(ZoneDwellAccumulator)

        for event in events:
            if event.session_id is None or event.zone_id is None:
                continue

            key = (event.session_id, event.zone_id)
            if event.event_type == EventType.ZONE_ENTERED:
                active_entries[key] = event
                continue

            entered_event = active_entries.pop(key, None)
            if entered_event is None:
                continue

            dwell_seconds = int((event.timestamp - entered_event.timestamp).total_seconds())
            if dwell_seconds < 0:
                continue

            accumulator = accumulators[event.zone_id]
            accumulator.visits += 1
            accumulator.total_seconds += dwell_seconds

        return [
            ZoneDwellMetric(
                zone_id=zone_id,
                visits=accumulator.visits,
                average_dwell_seconds=round(accumulator.total_seconds / accumulator.visits, 2),
                total_dwell_seconds=accumulator.total_seconds,
            )
            for zone_id, accumulator in sorted(accumulators.items())
            if accumulator.visits > 0
        ]

    def _queue_terminal_count(self, store_id: str, event_type: EventType) -> int:
        return self._scalar_int(
            select(func.count(Event.id))
            .where(Event.store_id == store_id)
            .where(Event.event_type == event_type)
        )

    def _scalar_int(self, statement) -> int:
        return int(self.db.scalar(statement) or 0)

    @staticmethod
    def _rate(numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return round(numerator / denominator, 4)
