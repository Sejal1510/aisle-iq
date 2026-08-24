from __future__ import annotations

from sqlalchemy.orm import Session

from app.schemas.analytics import (
    StoreFunnelResponse,
    StoreMetricsResponse,
    ZoneDwellMetric,
)
from app.schemas.insights import StoreInsight, StoreInsightsResponse
from app.services.analytics_service import AnalyticsService

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


class InsightsService:
    """Convert deterministic analytics thresholds into retail recommendations."""

    def __init__(self, db: Session):
        self.db = db
        self.analytics = AnalyticsService(db)

    def get_store_insights(self, store_id: str) -> StoreInsightsResponse:
        metrics = self.analytics.get_store_metrics(store_id)
        funnel = self.analytics.get_store_funnel(store_id)
        insights: list[StoreInsight] = []

        insights.extend(self._queue_insights(metrics))
        insights.extend(self._zone_insights(metrics))
        insights.extend(self._conversion_insights(metrics, funnel))
        insights.extend(self._visitor_insights(metrics))
        insights.extend(self._revenue_insights(metrics))

        return StoreInsightsResponse(
            store_id=store_id,
            insights=sorted(insights, key=lambda insight: (SEVERITY_ORDER[insight.severity], insight.category, insight.title)),
        )

    def _queue_insights(self, metrics: StoreMetricsResponse) -> list[StoreInsight]:
        insights: list[StoreInsight] = []
        abandonment = metrics.queue_abandonment_rate
        if abandonment >= 0.30:
            insights.append(
                StoreInsight(
                    category="Queue",
                    severity="HIGH",
                    title="Queue Abandonment Risk",
                    explanation=f"{_percent(abandonment)} of visitors abandon the billing queue.",
                    recommendation="Open an additional billing counter during peak periods and monitor queue length near checkout.",
                )
            )
        elif abandonment >= 0.15:
            insights.append(
                StoreInsight(
                    category="Queue",
                    severity="MEDIUM",
                    title="Queue Abandonment Watch",
                    explanation=f"{_percent(abandonment)} of visitors abandon the billing queue.",
                    recommendation="Review cashier coverage during busy windows before abandonment becomes severe.",
                )
            )

        wait = metrics.average_queue_wait_seconds
        if wait >= 120:
            insights.append(
                StoreInsight(
                    category="Queue",
                    severity="HIGH",
                    title="Long Queue Wait Time",
                    explanation=f"Average queue wait is {_duration(wait)}, above the 2 minute action threshold.",
                    recommendation="Add checkout capacity or move staff to billing when waits exceed the threshold.",
                )
            )
        elif wait >= 60:
            insights.append(
                StoreInsight(
                    category="Queue",
                    severity="MEDIUM",
                    title="Queue Wait Time Building",
                    explanation=f"Average queue wait is {_duration(wait)}, above the 1 minute watch threshold.",
                    recommendation="Prepare flexible staff coverage for the billing area during similar traffic periods.",
                )
            )
        return insights

    def _zone_insights(self, metrics: StoreMetricsResponse) -> list[StoreInsight]:
        zones = metrics.zone_dwell_metrics
        if not zones:
            return [
                StoreInsight(
                    category="Zone",
                    severity="LOW",
                    title="No Zone Dwell Signal",
                    explanation="No complete zone entry/exit dwell pairs are available for this store.",
                    recommendation="Check camera coverage and zone calibration before making layout decisions.",
                )
            ]

        insights: list[StoreInsight] = []
        average_visits = sum(zone.visits for zone in zones) / len(zones)
        average_dwell = sum(zone.average_dwell_seconds for zone in zones) / len(zones)
        for zone in zones:
            if zone.visits >= max(3, average_visits * 1.5):
                insights.append(
                    StoreInsight(
                        category="Zone",
                        severity="MEDIUM",
                        title=f"Hot Zone: {zone.zone_id}",
                        explanation=f"{zone.zone_id} attracts {zone.visits} dwell visits, above the store zone average.",
                        recommendation="Keep this zone well stocked and consider placing high-margin products nearby.",
                    )
                )
            if zone.average_dwell_seconds >= 300 or (average_dwell > 0 and zone.average_dwell_seconds >= average_dwell * 1.5 and zone.average_dwell_seconds >= 60):
                insights.append(
                    StoreInsight(
                        category="Zone",
                        severity="MEDIUM",
                        title=f"High Dwell Zone: {zone.zone_id}",
                        explanation=f"Average dwell in {zone.zone_id} is {_duration(zone.average_dwell_seconds)}.",
                        recommendation="Review whether shoppers are engaged, blocked, or waiting for assistance in this area.",
                    )
                )

        if len(zones) >= 2:
            for zone in zones:
                if zone.visits <= max(1, average_visits * 0.35):
                    insights.append(
                        StoreInsight(
                            category="Zone",
                            severity="LOW",
                            title=f"Dead Zone: {zone.zone_id}",
                            explanation=f"{zone.zone_id} has only {zone.visits} dwell visits compared with an average of {average_visits:.1f}.",
                            recommendation="Review signage, assortment, and sightline visibility for this low-traffic zone.",
                        )
                    )
        return insights

    def _conversion_insights(
        self,
        metrics: StoreMetricsResponse,
        funnel: StoreFunnelResponse,
    ) -> list[StoreInsight]:
        insights: list[StoreInsight] = []
        conversion = metrics.conversion_rate
        if conversion >= 0.35:
            insights.append(
                StoreInsight(
                    category="Conversion",
                    severity="LOW",
                    title="High Conversion Store",
                    explanation=f"Store conversion is {_percent(conversion)}, above the strong-performance threshold.",
                    recommendation="Capture the staffing, assortment, and queue practices from this store as a benchmark.",
                )
            )
        elif metrics.total_visitors >= 5 and conversion < 0.10:
            insights.append(
                StoreInsight(
                    category="Conversion",
                    severity="HIGH",
                    title="Low Conversion Store",
                    explanation=f"Store conversion is {_percent(conversion)} despite {metrics.total_visitors} customer visits.",
                    recommendation="Inspect product availability, assisted selling coverage, and checkout friction.",
                )
            )
        elif metrics.total_visitors >= 5 and conversion < 0.20:
            insights.append(
                StoreInsight(
                    category="Conversion",
                    severity="MEDIUM",
                    title="Conversion Below Target",
                    explanation=f"Store conversion is {_percent(conversion)}, below the 20% watch threshold.",
                    recommendation="Review the funnel and focus on the step with the largest visitor drop-off.",
                )
            )

        for step in funnel.steps[1:]:
            if step.rate_from_previous is not None and step.rate_from_previous < 0.60 and step.count > 0:
                insights.append(
                    StoreInsight(
                        category="Conversion",
                        severity="MEDIUM",
                        title=f"Funnel Drop-Off: {_title_step(step.step)}",
                        explanation=f"Only {_percent(step.rate_from_previous)} of visitors from the previous step reach {_title_step(step.step)}.",
                        recommendation="Investigate this funnel step for staffing, navigation, or checkout bottlenecks.",
                    )
                )
        return insights

    def _visitor_insights(self, metrics: StoreMetricsResponse) -> list[StoreInsight]:
        insights: list[StoreInsight] = []
        if metrics.unique_visitors:
            grouped_visitors = max(0, metrics.unique_visitors - metrics.solo_visitors)
            group_share = grouped_visitors / metrics.unique_visitors
            if group_share >= 0.50:
                insights.append(
                    StoreInsight(
                        category="Visitor",
                        severity="MEDIUM",
                        title="High Group Traffic",
                        explanation=f"{_percent(group_share)} of customers are assigned to shopping groups.",
                        recommendation="Use group-friendly assistance and keep high-interest zones easy to browse together.",
                    )
                )

            staff_ratio = metrics.staff_visitors / metrics.unique_visitors
            if staff_ratio >= 0.25:
                insights.append(
                    StoreInsight(
                        category="Visitor",
                        severity="MEDIUM",
                        title="High Staff-To-Customer Ratio",
                        explanation=f"Inferred staff count is {_percent(staff_ratio)} of customer visitors.",
                        recommendation="Check whether staff presence is supporting conversion or occupying customer-facing zones.",
                    )
                )
        return insights

    def _revenue_insights(self, metrics: StoreMetricsResponse) -> list[StoreInsight]:
        insights: list[StoreInsight] = []
        if metrics.attributed_transactions > 0:
            value_per_transaction = metrics.attributed_revenue / metrics.attributed_transactions
            if value_per_transaction >= 500:
                insights.append(
                    StoreInsight(
                        category="Revenue",
                        severity="LOW",
                        title="High-Value Transaction Mix",
                        explanation=f"Average attributed transaction value is INR {value_per_transaction:.0f}.",
                        recommendation="Protect availability for premium products and replicate this selling pattern in similar stores.",
                    )
                )

        opportunity_zones = self._revenue_opportunity_zones(metrics.zone_dwell_metrics, metrics.conversion_rate)
        for zone in opportunity_zones:
            insights.append(
                StoreInsight(
                    category="Revenue",
                    severity="MEDIUM",
                    title=f"Revenue Opportunity Zone: {zone.zone_id}",
                    explanation=f"{zone.zone_id} has {_duration(zone.average_dwell_seconds)} average dwell but store conversion is {_percent(metrics.conversion_rate)}.",
                    recommendation="Add assisted selling, sampling, or clearer product calls-to-action in this zone.",
                )
            )
        return insights

    @staticmethod
    def _revenue_opportunity_zones(
        zones: list[ZoneDwellMetric],
        conversion_rate: float,
    ) -> list[ZoneDwellMetric]:
        if conversion_rate >= 0.25:
            return []
        return [
            zone
            for zone in zones
            if zone.visits >= 2 and zone.average_dwell_seconds >= 60
        ][:2]


def _percent(value: float) -> str:
    return f"{round(value * 100)}%"


def _duration(seconds: float) -> str:
    rounded = round(seconds)
    if rounded < 60:
        return f"{rounded}s"
    minutes = rounded // 60
    remainder = rounded % 60
    return f"{minutes}m {remainder}s" if remainder else f"{minutes}m"


def _title_step(step: str) -> str:
    return step.replace("_", " ").title()
