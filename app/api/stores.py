from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.analytics import (
    StoreAnomaliesResponse,
    StoreFunnelResponse,
    StoreHeatmapResponse,
    StoreMetricsResponse,
)
from app.schemas.insights import StoreInsightsResponse
from app.schemas.live_analytics import (
    CurrentOccupancyResponse,
    CurrentQueueResponse,
    HourlyFootfallResponse,
    HourlyQueueActivityResponse,
    OccupancyHistoryResponse,
    PeakHoursResponse,
    PeriodComparisonResponse,
    QueueMetricsResponse,
)
from app.schemas.path_analytics import StorePathAnalyticsResponse
from app.services.analytics_service import AnalyticsService
from app.services.anomaly_service import AnomalyService
from app.services.comparison_service import ComparisonService
from app.services.heatmap_service import HeatmapService
from app.services.insights_service import InsightsService
from app.services.occupancy_service import OccupancyService
from app.services.path_analytics_service import PathAnalyticsService
from app.services.peak_hour_service import PeakHourService
from app.services.queue_service import QueueService
from app.services.time_series_service import TimeSeriesService

router = APIRouter()


def _run_ranged(callable_) -> object:
    """Domain-level range validation (end <= start, non-positive bucket, etc.)
    raises ValueError from the P3 services; surface it as 400 rather than an
    unhandled 500, consistent with the ValueError->400 convention already
    used by the ingestion routes."""
    try:
        return callable_()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/stores/{store_id}/metrics", response_model=StoreMetricsResponse)
def get_store_metrics(store_id: str, db: Session = Depends(get_db)) -> StoreMetricsResponse:
    return AnalyticsService(db).get_store_metrics(store_id)


@router.get("/stores/{store_id}/funnel", response_model=StoreFunnelResponse)
def get_store_funnel(store_id: str, db: Session = Depends(get_db)) -> StoreFunnelResponse:
    return AnalyticsService(db).get_store_funnel(store_id)


@router.get("/stores/{store_id}/heatmap", response_model=StoreHeatmapResponse)
def get_store_heatmap(store_id: str, db: Session = Depends(get_db)) -> StoreHeatmapResponse:
    return HeatmapService(db).get_store_heatmap(store_id)


@router.get("/stores/{store_id}/anomalies", response_model=StoreAnomaliesResponse)
def get_store_anomalies(store_id: str, db: Session = Depends(get_db)) -> StoreAnomaliesResponse:
    return AnomalyService(db).get_store_anomalies(store_id)


@router.get("/stores/{store_id}/insights", response_model=StoreInsightsResponse)
def get_store_insights(store_id: str, db: Session = Depends(get_db)) -> StoreInsightsResponse:
    return InsightsService(db).get_store_insights(store_id)


@router.get("/stores/{store_id}/paths", response_model=StorePathAnalyticsResponse)
def get_store_paths(store_id: str, db: Session = Depends(get_db)) -> StorePathAnalyticsResponse:
    return PathAnalyticsService(db).get_store_paths(store_id)


@router.get("/stores/{store_id}/occupancy/current", response_model=CurrentOccupancyResponse)
def get_current_occupancy(
    store_id: str, as_of: datetime | None = Query(None), db: Session = Depends(get_db)
) -> CurrentOccupancyResponse:
    return OccupancyService(db).current_occupancy(store_id, as_of=as_of)


@router.get("/stores/{store_id}/occupancy/history", response_model=OccupancyHistoryResponse)
def get_occupancy_history(
    store_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    bucket_minutes: int = Query(60, gt=0),
    db: Session = Depends(get_db),
) -> OccupancyHistoryResponse:
    return _run_ranged(
        lambda: OccupancyService(db).occupancy_series(store_id, start, end, bucket_minutes=bucket_minutes)
    )


@router.get("/stores/{store_id}/queue/current", response_model=CurrentQueueResponse)
def get_current_queue(
    store_id: str, as_of: datetime | None = Query(None), db: Session = Depends(get_db)
) -> CurrentQueueResponse:
    return QueueService(db).current_queue(store_id, as_of=as_of)


@router.get("/stores/{store_id}/queue/metrics", response_model=QueueMetricsResponse)
def get_queue_metrics(
    store_id: str, start: datetime = Query(...), end: datetime = Query(...), db: Session = Depends(get_db)
) -> QueueMetricsResponse:
    return _run_ranged(lambda: QueueService(db).queue_metrics(store_id, start, end))


@router.get("/stores/{store_id}/footfall/hourly", response_model=HourlyFootfallResponse)
def get_hourly_footfall(
    store_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    bucket_minutes: int = Query(60, gt=0),
    db: Session = Depends(get_db),
) -> HourlyFootfallResponse:
    return _run_ranged(
        lambda: TimeSeriesService(db).hourly_footfall(store_id, start, end, bucket_minutes=bucket_minutes)
    )


@router.get("/stores/{store_id}/queue/hourly", response_model=HourlyQueueActivityResponse)
def get_hourly_queue_activity(
    store_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    bucket_minutes: int = Query(60, gt=0),
    db: Session = Depends(get_db),
) -> HourlyQueueActivityResponse:
    return _run_ranged(
        lambda: TimeSeriesService(db).hourly_queue_activity(store_id, start, end, bucket_minutes=bucket_minutes)
    )


@router.get("/stores/{store_id}/peak-hours", response_model=PeakHoursResponse)
def get_peak_hours(
    store_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    bucket_minutes: int = Query(60, gt=0),
    db: Session = Depends(get_db),
) -> PeakHoursResponse:
    return _run_ranged(
        lambda: PeakHourService(db).peak_hours(store_id, start, end, bucket_minutes=bucket_minutes)
    )


@router.get("/stores/{store_id}/comparison", response_model=PeriodComparisonResponse)
def get_period_comparison(
    store_id: str, start: datetime = Query(...), end: datetime = Query(...), db: Session = Depends(get_db)
) -> PeriodComparisonResponse:
    return _run_ranged(lambda: ComparisonService(db).compare(store_id, start, end))


@router.get("/stores/{store_id}/layout")
def get_store_layout(store_id: str) -> FileResponse:
    layout_path = HeatmapService.layout_image_path(store_id)
    if layout_path is None:
        raise HTTPException(status_code=404, detail="Store layout image not found")
    return FileResponse(layout_path, media_type="image/png")
