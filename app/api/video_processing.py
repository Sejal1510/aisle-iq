import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.security import require_store_role
from app.core.storage import VIDEOS_DIR, StorageError
from app.db.session import get_db
from app.models.auth import StoreAccess
from app.models.enums import Role
from app.models.video_processing import VideoProcessingJob
from app.schemas.video_processing import (
    VideoProcessingJobCreateRequest,
    VideoProcessingJobListResponse,
    VideoProcessingJobOut,
    VideoRunSummaryOut,
)
from app.services.video_processing_service import (
    VideoProcessingError,
    VideoProcessingService,
)
from pipeline.video.annotation import annotated_output_paths, read_run_summary

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
    except (VideoProcessingError, StorageError) as exc:
        # StorageError: a camera whose stored video_path points outside the
        # video storage directory (e.g. one configured by the legacy setup
        # script) -- a request problem with a clear fix (upload the video
        # through the app), not a server error.
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


@router.get("/stores/{store_id}/video-processing/{job_id}/annotated-video")
def get_annotated_video(
    store_id: str, job_id: str, db: Session = Depends(get_db), access: StoreAccess = _read_access
) -> FileResponse:
    """The run's annotated video (person boxes + track ids drawn by the
    worker). Same ANALYST floor as the job itself; the file is resolved from
    the job id, never from a client-supplied path."""
    job = VideoProcessingService(db).get_job(store_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Video processing job not found.")
    located = _annotated_video(job)
    if located is None:
        raise HTTPException(status_code=404, detail="No annotated video is available for this job.")
    path, content_type = located
    return FileResponse(path, media_type=content_type)


def _annotated_video(job: VideoProcessingJob) -> tuple[Path, str] | None:
    output_stem, summary_path = annotated_output_paths(VIDEOS_DIR, job.store_id, job.id)
    summary = read_run_summary(summary_path)
    if not summary or not summary.get("annotated_file"):
        return None
    path = output_stem.parent / Path(summary["annotated_file"]).name
    if not path.is_file():
        return None
    return path, summary.get("annotated_content_type") or "video/mp4"


def _run_summary(job: VideoProcessingJob) -> VideoRunSummaryOut | None:
    _output_stem, summary_path = annotated_output_paths(VIDEOS_DIR, job.store_id, job.id)
    summary = read_run_summary(summary_path)
    if not summary:
        return None
    try:
        return VideoRunSummaryOut.model_validate(summary)
    except ValueError:
        return None


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
        annotated_video_available=_annotated_video(job) is not None,
        run_summary=_run_summary(job),
    )
