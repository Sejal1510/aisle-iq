from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import grant_store_access, require_store_role
from app.db.session import get_db
from app.models.auth import StoreAccess, User
from app.models.enums import Role
from app.schemas.analytics import (
    StoreAnomaliesResponse,
    StoreFunnelResponse,
    StoreHeatmapResponse,
    StoreMetricsResponse,
)
from app.schemas.auth import StoreAccessGrantRequest, StoreAccessGrantResponse
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

# P4.4: every existing analytics/dashboard route below requires at least
# ANALYST access to the store named in its path -- any granted role (ADMIN,
# MANAGER, ANALYST) satisfies it. Only the access-grant endpoint itself
# requires ADMIN. See app.core.security.require_store_role.
_read_access = Depends(require_store_role(Role.ANALYST))
_admin_access = Depends(require_store_role(Role.ADMIN))


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
def get_store_metrics(
    store_id: str, db: Session = Depends(get_db), access: StoreAccess = _read_access
) -> StoreMetricsResponse:
    return AnalyticsService(db).get_store_metrics(store_id)


@router.get("/stores/{store_id}/funnel", response_model=StoreFunnelResponse)
def get_store_funnel(
    store_id: str, db: Session = Depends(get_db), access: StoreAccess = _read_access
) -> StoreFunnelResponse:
    return AnalyticsService(db).get_store_funnel(store_id)


@router.get("/stores/{store_id}/heatmap", response_model=StoreHeatmapResponse)
def get_store_heatmap(
    store_id: str, db: Session = Depends(get_db), access: StoreAccess = _read_access
) -> StoreHeatmapResponse:
    return HeatmapService(db).get_store_heatmap(store_id)


@router.get("/stores/{store_id}/anomalies", response_model=StoreAnomaliesResponse)
def get_store_anomalies(
    store_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
    db: Session = Depends(get_db),
    access: StoreAccess = _read_access,
) -> StoreAnomaliesResponse:
    """P4.3: start/end are optional and additive. Omitting both keeps this
    endpoint's pre-P4.3 behavior byte-identical (static rules only) -- a hard
    backward-compatibility requirement. Supplying both additionally runs
    trend (comparison-driven) rules. Supplying exactly one is a client error,
    same convention as the ValueError->400 range endpoints elsewhere in this
    file, rather than silently ignoring the stray parameter.

    Deliberately a plain `= None` default, not `Query(None)`: this whole test
    suite's established convention calls route functions directly (bypassing
    FastAPI's dependency injection), and Query(None) resolves to a live
    fastapi.Query sentinel object -- not real None -- when called that way,
    which silently broke the pre-existing (pre-P4.3)
    test_anomalies_endpoint_returns_operational_contract the moment these
    params were added. A plain None default behaves identically for real
    HTTP requests and is directly callable, which every other endpoint in
    this file's existing test suite already relies on.
    """
    if (start is None) != (end is None):
        raise HTTPException(status_code=400, detail="start and end must be provided together")
    return _run_ranged(lambda: AnomalyService(db).get_store_anomalies(store_id, start=start, end=end))


@router.get("/stores/{store_id}/insights", response_model=StoreInsightsResponse)
def get_store_insights(
    store_id: str, db: Session = Depends(get_db), access: StoreAccess = _read_access
) -> StoreInsightsResponse:
    return InsightsService(db).get_store_insights(store_id)


@router.get("/stores/{store_id}/paths", response_model=StorePathAnalyticsResponse)
def get_store_paths(
    store_id: str, db: Session = Depends(get_db), access: StoreAccess = _read_access
) -> StorePathAnalyticsResponse:
    return PathAnalyticsService(db).get_store_paths(store_id)


@router.get("/stores/{store_id}/occupancy/current", response_model=CurrentOccupancyResponse)
def get_current_occupancy(
    store_id: str,
    as_of: datetime | None = Query(None),
    db: Session = Depends(get_db),
    access: StoreAccess = _read_access,
) -> CurrentOccupancyResponse:
    return OccupancyService(db).current_occupancy(store_id, as_of=as_of)


@router.get("/stores/{store_id}/occupancy/history", response_model=OccupancyHistoryResponse)
def get_occupancy_history(
    store_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    bucket_minutes: int = Query(60, gt=0),
    db: Session = Depends(get_db),
    access: StoreAccess = _read_access,
) -> OccupancyHistoryResponse:
    return _run_ranged(
        lambda: OccupancyService(db).occupancy_series(store_id, start, end, bucket_minutes=bucket_minutes)
    )


@router.get("/stores/{store_id}/queue/current", response_model=CurrentQueueResponse)
def get_current_queue(
    store_id: str,
    as_of: datetime | None = Query(None),
    db: Session = Depends(get_db),
    access: StoreAccess = _read_access,
) -> CurrentQueueResponse:
    return QueueService(db).current_queue(store_id, as_of=as_of)


@router.get("/stores/{store_id}/queue/metrics", response_model=QueueMetricsResponse)
def get_queue_metrics(
    store_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    db: Session = Depends(get_db),
    access: StoreAccess = _read_access,
) -> QueueMetricsResponse:
    return _run_ranged(lambda: QueueService(db).queue_metrics(store_id, start, end))


@router.get("/stores/{store_id}/footfall/hourly", response_model=HourlyFootfallResponse)
def get_hourly_footfall(
    store_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    bucket_minutes: int = Query(60, gt=0),
    db: Session = Depends(get_db),
    access: StoreAccess = _read_access,
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
    access: StoreAccess = _read_access,
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
    access: StoreAccess = _read_access,
) -> PeakHoursResponse:
    return _run_ranged(
        lambda: PeakHourService(db).peak_hours(store_id, start, end, bucket_minutes=bucket_minutes)
    )


@router.get("/stores/{store_id}/comparison", response_model=PeriodComparisonResponse)
def get_period_comparison(
    store_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    db: Session = Depends(get_db),
    access: StoreAccess = _read_access,
) -> PeriodComparisonResponse:
    return _run_ranged(lambda: ComparisonService(db).compare(store_id, start, end))


@router.get("/stores/{store_id}/layout")
def get_store_layout(store_id: str, access: StoreAccess = _read_access) -> FileResponse:
    layout_path = HeatmapService.layout_image_path(store_id)
    if layout_path is None:
        raise HTTPException(status_code=404, detail="Store layout image not found")
    return FileResponse(layout_path, media_type="image/png")


@router.post("/stores/{store_id}/access", response_model=StoreAccessGrantResponse, status_code=201)
def grant_access(
    store_id: str,
    payload: StoreAccessGrantRequest,
    db: Session = Depends(get_db),
    admin_access: StoreAccess = _admin_access,
) -> StoreAccessGrantResponse:
    """Grant (or update) an existing user's role for this store. ADMIN-only.
    Does not create users -- there is no self-service signup in this phase;
    users are provisioned by the offline bootstrap script (see README.md),
    and this endpoint only manages their per-store role once they exist."""
    target_user = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if target_user is None:
        raise HTTPException(status_code=404, detail=f"No user found with email '{payload.email}'.")

    grant_store_access(db, target_user.id, store_id, payload.role)
    db.commit()
    return StoreAccessGrantResponse(store_id=store_id, email=payload.email, role=payload.role)
