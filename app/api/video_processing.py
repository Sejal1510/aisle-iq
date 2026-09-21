import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import require_store_role
from app.db.session import get_db
from app.models.auth import StoreAccess
from app.models.enums import Role
from app.models.video_processing import VideoProcessingJob
from app.schemas.video_processing import (
    VideoProcessingJobCreateRequest,
    VideoProcessingJobListResponse,
    VideoProcessingJobOut,
)
from app.services.video_processing_service import (
    VideoProcessingError,
    VideoProcessingService,
)

router = APIRouter()

# Starting processing is an operational action on already-uploaded footage
# (MANAGER, the same floor app/api/replay.py uses for triggering a replay) --
# not the structural camera/video config app/api/onboarding.py reserves for
# ADMIN. Status/history is read-only and stays at the existing ANALYST floor
# every other analytics route in this project uses.
_run_access = Depends(require_store_role(Role.MANAGER))
_read_access = Depends(require_store_role(Role.ANALYST))


def _run(callable_):
    try:
        return callable_()
    except VideoProcessingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/stores/{store_id}/video-processing", response_model=VideoProcessingJobOut, status_code=201)
def create_video_processing_job(
    store_id: str,
    payload: VideoProcessingJobCreateRequest,
    db: Session = Depends(get_db),
    access: StoreAccess = _run_access,
) -> VideoProcessingJobOut:
    """Queue a camera's uploaded video for processing.

    A duplicate request for a camera that already has an active (PENDING/
    RUNNING) job is a safe no-op, not an error -- VideoProcessingService.
    create_job already returns that existing job instead of raising (see its
    own docstring), so this route always responds 201 with whichever job
    -- new or pre-existing -- is now the camera's active one.
    """
    job = _run(
        lambda: VideoProcessingService(db).create_job(
            store_id,
            payload.camera_id,
            requested_by_user_id=access.user_id,
        )
    )
    db.commit()
    return _job_to_out(job)


@router.get("/stores/{store_id}/video-processing", response_model=VideoProcessingJobListResponse)
def list_video_processing_jobs(
    store_id: str, db: Session = Depends(get_db), access: StoreAccess = _read_access
) -> VideoProcessingJobListResponse:
    jobs = VideoProcessingService(db).list_jobs(store_id)
    return VideoProcessingJobListResponse(store_id=store_id, jobs=[_job_to_out(job) for job in jobs])


@router.get("/stores/{store_id}/video-processing/{job_id}", response_model=VideoProcessingJobOut)
def get_video_processing_job(
    store_id: str, job_id: str, db: Session = Depends(get_db), access: StoreAccess = _read_access
) -> VideoProcessingJobOut:
    job = VideoProcessingService(db).get_job(store_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video processing job not found.")
    return _job_to_out(job)


def _job_to_out(job: VideoProcessingJob) -> VideoProcessingJobOut:
    return VideoProcessingJobOut(
        id=job.id,
        store_id=job.store_id,
        camera_id=job.camera_id,
        status=job.status,
        total_events=job.total_events,
        processed_events=job.processed_events,
        accepted_events=job.accepted_events,
        duplicate_events=job.duplicate_events,
        failed_events=job.failed_events,
        error_message=job.error_message,
        error_details=json.loads(job.error_details_json) if job.error_details_json else None,
        claimed_at=job.claimed_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        created_at=job.created_at,
    )
