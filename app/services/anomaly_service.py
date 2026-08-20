from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import CorrelationStatus, EventType
from app.models.event import Event
from app.models.pos import TransactionCorrelation
from app.models.tracking import VisitSession
from app.schemas.analytics import StoreAnomaliesResponse, StoreAnomaly
from app.schemas.live_analytics import PeriodMetrics
from app.services.analytics_service import AnalyticsService
from app.services.comparison_service import ComparisonService

_SEVERITY_ORDER = {"CRITICAL": 0, "WARN": 1, "INFO": 2}

# P4.3 trend-significance thresholds. Hand-chosen constants, mirroring the
# style _queue_spike already used (a numeric magnitude crossing a bar), not
# learned or configurable. Validated against the real P4.1 demo data (see
# tests/test_anomaly_service.py) before being finalized -- see each
# constant's comment for what specifically shaped its value.
_FOOTFALL_MIN_SAMPLE = 10
_FOOTFALL_PERCENT_GATE = 0.30
_FOOTFALL_ABSOLUTE_GATE = 8
_FOOTFALL_CRITICAL_DROP_PERCENT = 0.50

# Occupancy deliberately has NO percent gate. Real P4.1 data (ST1002,
# 2026-06-01 vs 2026-06-02) moves average_occupancy 0.12 -> 0.72, a +500%
# change from a tiny baseline -- a percent-only gate would flag that as an
# extreme event when it is really a moderate, real increase. An absolute gate
# in "average concurrent visitors" units is what correctly separates a small
# baseline's inflated percentage from an actually large change.
_OCCUPANCY_ABSOLUTE_GATE = 0.3

_QUEUE_MIN_SAMPLE = 5
_ABANDONMENT_POINTS_GATE = 0.10
_ABANDONMENT_CRITICAL_POINTS = 0.20
_WAIT_TIME_PERCENT_GATE = 0.25
_WAIT_TIME_ABSOLUTE_GATE_SECONDS = 30

_TRAFFIC_CLUSTER = frozenset({"footfall", "average_occupancy"})
_QUEUE_HEALTH_CLUSTER = frozenset({"queue_abandonment_rate", "average_queue_wait_seconds"})

# When a cluster's signals are grouped into one alert, this fixes which
# metric leads the headline -- a deliberate, fixed preference, not a ranking
# by raw percent_change. Found necessary during validation against the real
# P4.1 demo data: ST1002's traffic cluster has average_occupancy moving
# 0.12 -> 0.72 (+500%, from the small baseline the absolute gate above exists
# to discount) alongside footfall moving 40 -> 53 (+32.5%). Ranking by
# abs(percent_change) picked occupancy as the "primary" signal -- the exact
# small-baseline distortion the occupancy gate was built to avoid, just
# re-introduced one step later at the grouping stage. Footfall is also the
# more legible, headline-friendly number for a general reader. Abandonment
# rate is preferred over wait time in the queue-health cluster as the more
# directly actionable of the two.
_CLUSTER_PRIMARY_PREFERENCE = {
    "traffic": ("footfall", "average_occupancy"),
    "queue_health": ("queue_abandonment_rate", "average_queue_wait_seconds"),
}

_METRIC_LABELS = {
    "footfall": "Footfall",
    "average_occupancy": "Average occupancy",
    "queue_abandonment_rate": "Queue abandonment rate",
    "average_queue_wait_seconds": "Average queue wait time",
}


@dataclass(frozen=True)
class _TrendSignal:
    """One metric's period-over-period change that has already cleared its
    significance gate. Not yet an alert -- clustering may merge several
    signals into one StoreAnomaly, or this may stand alone."""

    metric: str
    current: float
    previous: float
    absolute_change: float
    percent_change: float | None
    direction: str  # "increase" | "decrease"
    cluster: str | None
    bad_direction: bool  # whether this direction is operationally concerning


class AnomalyService:
    def __init__(self, db: Session):
        self.db = db
        self.analytics = AnalyticsService(db)
        self.comparison = ComparisonService(db)

    def get_store_anomalies(
        self, store_id: str, start: datetime | None = None, end: datetime | None = None
    ) -> StoreAnomaliesResponse:
        """Static (all-time) rules always run, unchanged from before P4.3.
        Trend (comparison-driven) rules run only when both start and end are
        supplied -- omitting them keeps this endpoint byte-identical to its
        pre-P4.3 behavior, which is a hard backward-compatibility requirement,
        not just an implementation convenience."""
        anomalies: list[StoreAnomaly] = []
        anomalies.extend(self._queue_spike(store_id))
        anomalies.extend(self._conversion_drop(store_id))
        anomalies.extend(self._dead_zone(store_id))

        if start is not None and end is not None:
            anomalies.extend(self._trend_anomalies(store_id, start, end))

        return StoreAnomaliesResponse(store_id=store_id, anomalies=self._prioritize(anomalies))

    @staticmethod
    def _prioritize(anomalies: list[StoreAnomaly]) -> list[StoreAnomaly]:
        return sorted(anomalies, key=lambda anomaly: _SEVERITY_ORDER.get(anomaly.severity, 99))

    # ------------------------------------------------------------------
    # Pre-existing static (all-time) rules -- unchanged from before P4.3.
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # P4.3 trend (comparison-driven) rules.
    # ------------------------------------------------------------------

    def _trend_anomalies(self, store_id: str, start: datetime, end: datetime) -> list[StoreAnomaly]:
        comparison = self.comparison.compare(store_id, start, end)
        current, previous = comparison.current, comparison.previous

        signals = [
            signal
            for signal in (
                self._footfall_signal(current, previous),
                self._occupancy_signal(current, previous),
                self._abandonment_signal(current, previous),
                self._wait_time_signal(current, previous),
            )
            if signal is not None
        ]
        if not signals:
            return []

        return self._cluster_and_build(signals, current, previous)

    def _footfall_signal(self, current: PeriodMetrics, previous: PeriodMetrics) -> _TrendSignal | None:
        if current.footfall < _FOOTFALL_MIN_SAMPLE or previous.footfall < _FOOTFALL_MIN_SAMPLE:
            return None
        absolute = current.footfall - previous.footfall
        percent = (absolute / previous.footfall) if previous.footfall else None
        if percent is None or abs(percent) < _FOOTFALL_PERCENT_GATE or abs(absolute) < _FOOTFALL_ABSOLUTE_GATE:
            return None
        direction = "increase" if absolute > 0 else "decrease"
        return _TrendSignal(
            metric="footfall",
            current=current.footfall,
            previous=previous.footfall,
            absolute_change=absolute,
            percent_change=percent,
            direction=direction,
            cluster="traffic",
            bad_direction=direction == "decrease",
        )

    def _occupancy_signal(self, current: PeriodMetrics, previous: PeriodMetrics) -> _TrendSignal | None:
        absolute = round(current.average_occupancy - previous.average_occupancy, 4)
        if abs(absolute) < _OCCUPANCY_ABSOLUTE_GATE:
            return None
        percent = (absolute / previous.average_occupancy) if previous.average_occupancy else None
        direction = "increase" if absolute > 0 else "decrease"
        return _TrendSignal(
            metric="average_occupancy",
            current=current.average_occupancy,
            previous=previous.average_occupancy,
            absolute_change=absolute,
            percent_change=percent,
            direction=direction,
            cluster="traffic",
            bad_direction=direction == "decrease",
        )

    def _abandonment_signal(self, current: PeriodMetrics, previous: PeriodMetrics) -> _TrendSignal | None:
        current_volume = current.queue_completed + current.queue_abandoned
        previous_volume = previous.queue_completed + previous.queue_abandoned
        if current_volume < _QUEUE_MIN_SAMPLE or previous_volume < _QUEUE_MIN_SAMPLE:
            return None
        absolute = round(current.queue_abandonment_rate - previous.queue_abandonment_rate, 4)
        if absolute < _ABANDONMENT_POINTS_GATE:  # only the increase direction is a rule
            return None
        percent = (
            (absolute / previous.queue_abandonment_rate) if previous.queue_abandonment_rate else None
        )
        return _TrendSignal(
            metric="queue_abandonment_rate",
            current=current.queue_abandonment_rate,
            previous=previous.queue_abandonment_rate,
            absolute_change=absolute,
            percent_change=percent,
            direction="increase",
            cluster="queue_health",
            bad_direction=True,
        )

    def _wait_time_signal(self, current: PeriodMetrics, previous: PeriodMetrics) -> _TrendSignal | None:
        current_volume = current.queue_completed + current.queue_abandoned
        previous_volume = previous.queue_completed + previous.queue_abandoned
        if current_volume < _QUEUE_MIN_SAMPLE or previous_volume < _QUEUE_MIN_SAMPLE:
            return None
        if current.average_queue_wait_seconds is None or previous.average_queue_wait_seconds is None:
            return None
        absolute = current.average_queue_wait_seconds - previous.average_queue_wait_seconds
        if absolute <= 0 or absolute < _WAIT_TIME_ABSOLUTE_GATE_SECONDS:  # only the increase direction is a rule
            return None
        percent = (absolute / previous.average_queue_wait_seconds) if previous.average_queue_wait_seconds else None
        if percent is None or percent < _WAIT_TIME_PERCENT_GATE:
            return None
        return _TrendSignal(
            metric="average_queue_wait_seconds",
            current=current.average_queue_wait_seconds,
            previous=previous.average_queue_wait_seconds,
            absolute_change=absolute,
            percent_change=percent,
            direction="increase",
            cluster="queue_health",
            bad_direction=True,
        )

    def _cluster_and_build(
        self, signals: list[_TrendSignal], current: PeriodMetrics, previous: PeriodMetrics
    ) -> list[StoreAnomaly]:
        """Group same-cluster, same-direction signals into one StoreAnomaly
        instead of emitting one card per metric -- see the module docstring
        in AnomalyService.get_store_anomalies and the P4.3 plan's clustering
        rule. Different clusters, or a cluster with only one qualifying
        signal, are never merged."""
        by_cluster_direction: dict[tuple[str | None, str], list[_TrendSignal]] = {}
        for signal in signals:
            key = (signal.cluster, signal.direction)
            by_cluster_direction.setdefault(key, []).append(signal)

        results: list[StoreAnomaly] = []
        for (cluster, direction), group in by_cluster_direction.items():
            if cluster is not None and len(group) > 1:
                results.append(self._build_grouped(cluster, direction, group, current, previous))
            else:
                for signal in group:
                    results.append(self._build_single(signal, current, previous))
        return results

    def _build_single(self, signal: _TrendSignal, current: PeriodMetrics, previous: PeriodMetrics) -> StoreAnomaly:
        severity = self._single_severity(signal)
        return StoreAnomaly(
            anomaly_type=f"trend_{signal.metric}_{signal.direction}",
            severity=severity,
            message=self._describe(signal),
            suggested_action=self._suggested_action(signal.cluster, signal.direction, grouped=False),
            metric_value=signal.current,
            metric=signal.metric,
            current_value=signal.current,
            comparison_value=signal.previous,
            absolute_change=signal.absolute_change,
            percent_change=signal.percent_change,
            current_period_start=current.start,
            current_period_end=current.end,
            previous_period_start=previous.start,
            previous_period_end=previous.end,
            related_signals=[],
        )

    def _build_grouped(
        self,
        cluster: str,
        direction: str,
        group: list[_TrendSignal],
        current: PeriodMetrics,
        previous: PeriodMetrics,
    ) -> StoreAnomaly:
        # Primary = the cluster's fixed preferred metric (see
        # _CLUSTER_PRIMARY_PREFERENCE), not the larger relative move -- percent
        # change is not comparable across metrics with very different
        # baselines. Its numbers become the structured fields; the rest
        # become non-causal related_signals text.
        preference = _CLUSTER_PRIMARY_PREFERENCE[cluster]
        primary, *others = sorted(group, key=lambda signal: preference.index(signal.metric))
        related = [
            f"{_METRIC_LABELS[other.metric]} also {other.direction}d over the same period "
            f"({self._format_value(other.metric, other.previous)} → {self._format_value(other.metric, other.current)})."
            for other in others
        ]
        anomaly_type = f"trend_traffic_{direction}" if cluster == "traffic" else "trend_queue_health_decline"
        severity = self._grouped_severity(cluster, direction, group)
        return StoreAnomaly(
            anomaly_type=anomaly_type,
            severity=severity,
            message=self._describe(primary),
            suggested_action=self._suggested_action(cluster, direction, grouped=True),
            metric_value=primary.current,
            metric=primary.metric,
            current_value=primary.current,
            comparison_value=primary.previous,
            absolute_change=primary.absolute_change,
            percent_change=primary.percent_change,
            current_period_start=current.start,
            current_period_end=current.end,
            previous_period_start=previous.start,
            previous_period_end=previous.end,
            related_signals=related,
        )

    @staticmethod
    def _single_severity(signal: _TrendSignal) -> str:
        if signal.cluster == "traffic":
            if not signal.bad_direction:
                return "INFO"
            if signal.metric == "footfall" and signal.percent_change is not None and abs(signal.percent_change) >= _FOOTFALL_CRITICAL_DROP_PERCENT:
                return "CRITICAL"
            return "WARN"
        # queue_health: only the increase direction ever reaches here.
        if signal.metric == "queue_abandonment_rate" and signal.absolute_change >= _ABANDONMENT_CRITICAL_POINTS:
            return "CRITICAL"
        return "WARN"

    @staticmethod
    def _grouped_severity(cluster: str, direction: str, group: list[_TrendSignal]) -> str:
        if cluster == "traffic":
            return "CRITICAL" if direction == "decrease" else "WARN"
        return "CRITICAL"  # both queue-health signals firing together is always the severe case

    @staticmethod
    def _suggested_action(cluster: str | None, direction: str, *, grouped: bool) -> str:
        if cluster == "traffic":
            if direction == "increase":
                return "Confirm staffing matches the higher traffic level for this period."
            return "Review marketing, promotions, or external factors for the lower traffic, and confirm entry sensors are reporting correctly."
        return "Open another billing counter and review checkout staffing until wait time and abandonment normalize."

    @staticmethod
    def _format_value(metric: str, value: float) -> str:
        if metric == "queue_abandonment_rate":
            return f"{value * 100:.1f}%"
        if metric == "average_queue_wait_seconds":
            return f"{value:.0f}s"
        if metric == "average_occupancy":
            return f"{value:.2f}"
        return f"{value:.0f}"

    def _describe(self, signal: _TrendSignal) -> str:
        label = _METRIC_LABELS[signal.metric]
        current_text = self._format_value(signal.metric, signal.current)
        previous_text = self._format_value(signal.metric, signal.previous)
        if signal.metric == "queue_abandonment_rate":
            points = abs(signal.absolute_change) * 100
            return (
                f"{label} increased by {points:.1f} percentage points compared with the previous "
                f"equivalent period ({previous_text} → {current_text})."
            )
        if signal.percent_change is not None:
            percent_text = f"{abs(signal.percent_change) * 100:.1f}%"
            return (
                f"{label} {signal.direction}d by {percent_text} ({previous_text} → {current_text}) "
                f"compared with the previous equivalent period."
            )
        return (
            f"{label} {signal.direction}d ({previous_text} → {current_text}) compared with the "
            f"previous equivalent period."
        )
