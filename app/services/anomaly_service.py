from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import CorrelationStatus, EventType
from app.models.event import Event
from app.models.pos import TransactionCorrelation
from app.models.tracking import VisitSession
from app.schemas.analytics import StoreAnomaliesResponse, StoreAnomaly
from app.services.analytics_service import AnalyticsService


class AnomalyService:
    def __init__(self, db: Session):
        self.db = db
        self.analytics = AnalyticsService(db)

    def get_store_anomalies(self, store_id: str) -> StoreAnomaliesResponse:
        anomalies: list[StoreAnomaly] = []
        anomalies.extend(self._queue_spike(store_id))
        anomalies.extend(self._conversion_drop(store_id))
        anomalies.extend(self._dead_zone(store_id))
        return StoreAnomaliesResponse(store_id=store_id, anomalies=anomalies)

    def _queue_spike(self, store_id: str) -> list[StoreAnomaly]:
        wait_seconds = self.analytics.average_queue_wait_seconds(store_id)
        abandonment_rate = self.analytics.queue_abandonment_rate(store_id)
        if wait_seconds < 120 and abandonment_rate < 0.30:
            return []
        severity = "CRITICAL" if wait_seconds >= 300 or abandonment_rate >= 0.50 else "WARN"
        return [
            StoreAnomaly(
                anomaly_type="queue_spike",
                severity=severity,
                message="Billing queue wait or abandonment is above the operating threshold.",
                suggested_action="Open another billing counter and move floor staff to checkout until the queue normalizes.",
                metric_value=max(wait_seconds, abandonment_rate),
            )
        ]

    def _conversion_drop(self, store_id: str) -> list[StoreAnomaly]:
        metrics = self.analytics.get_store_metrics(store_id)
        if metrics.total_visitors < 5 or metrics.conversion_rate >= 0.10:
            return []
        return [
            StoreAnomaly(
                anomaly_type="conversion_drop",
                severity="WARN",
                message="Current conversion is below the minimum healthy threshold.",
                suggested_action="Review assisted selling coverage and inspect funnel drop-off before checkout.",
                metric_value=metrics.conversion_rate,
            )
        ]

    def _dead_zone(self, store_id: str) -> list[StoreAnomaly]:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30)
        visited_zone_ids = set(
            self.db.scalars(
                select(Event.zone_id)
                .where(Event.store_id == store_id)
                .where(Event.zone_id.is_not(None))
                .where(Event.timestamp >= cutoff)
                .where(Event.event_type.in_([EventType.ZONE_ENTERED, EventType.ZONE_DWELL]))
                .distinct()
            ).all()
        )
        historical_zone_ids = set(
            self.db.scalars(
                select(Event.zone_id)
                .where(Event.store_id == store_id)
                .where(Event.zone_id.is_not(None))
                .distinct()
            ).all()
        )

        dead_zones = sorted(historical_zone_ids - visited_zone_ids)
        return [
            StoreAnomaly(
                anomaly_type="dead_zone",
                severity="INFO",
                message=f"No visits recorded in {zone_id} during the last 30 minutes.",
                suggested_action="Check camera coverage, sightlines, signage, and merchandising for this zone.",
                metric_value=0,
            )
            for zone_id in dead_zones[:5]
        ]

    def seven_day_conversion_rate(self, store_id: str) -> float:
        sessions = int(
            self.db.scalar(select(func.count(VisitSession.id)).where(VisitSession.store_id == store_id)) or 0
        )
        purchases = int(
            self.db.scalar(
                select(func.count(func.distinct(TransactionCorrelation.session_id)))
                .where(TransactionCorrelation.status == CorrelationStatus.MATCHED)
                .where(TransactionCorrelation.session_id.is_not(None))
            )
            or 0
        )
        return round(purchases / sessions, 4) if sessions else 0.0
