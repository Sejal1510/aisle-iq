from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.analytics import StoreAnomaliesResponse, StoreFunnelResponse, StoreHeatmapResponse, StoreMetricsResponse
from app.schemas.insights import StoreInsightsResponse
from app.schemas.path_analytics import StorePathAnalyticsResponse
from app.services.analytics_service import AnalyticsService
from app.services.anomaly_service import AnomalyService
from app.services.heatmap_service import HeatmapService
from app.services.insights_service import InsightsService
from app.services.path_analytics_service import PathAnalyticsService


router = APIRouter()


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


@router.get("/stores/{store_id}/layout")
def get_store_layout(store_id: str) -> FileResponse:
    layout_path = HeatmapService.layout_image_path(store_id)
    if layout_path is None:
        raise HTTPException(status_code=404, detail="Store layout image not found")
    return FileResponse(layout_path, media_type="image/png")
