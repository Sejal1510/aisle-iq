import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import require_store_role
from app.db.session import get_db
from app.models.auth import StoreAccess
from app.models.enums import Role
from app.models.replay import ReplayJob
from app.schemas.replay import ReplayCreateRequest, ReplayJobListResponse, ReplayJobOut
from app.services.replay_service import ReplayError, ReplayService

router = APIRouter()

# Replay mutates derived state the same way ingestion does -- MANAGER is the
# floor to trigger one (an operational action, not the structural store
# config app/api/onboarding.py reserves for ADMIN). Status/history is
# read-only and stays at the existing ANALYST floor every other analytics
# route in this project uses.
_run_access = Depends(require_store_role(Role.MANAGER))
_read_access = Depends(require_store_role(Role.ANALYST))


def _run(callable_):
    try:
        return callable_()
    except ReplayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/stores/{store_id}/replay", response_model=ReplayJobOut, status_code=201)
def create_replay(
    store_id: str,
    payload: ReplayCreateRequest,
    db: Session = Depends(get_db),
    access: StoreAccess = _run_access,
) -> ReplayJobOut:
    """Create and synchronously run a replay job for this store.

    Synchronous, not fire-and-forget: this project has no background task
    runner, so the response already carries the finished job's full status/
    counts/errors -- there is nothing further a client must poll for unless
    it wants to look the job up again later (see get_replay/list_replays).
    """
    job = _run(
        lambda: ReplayService(db).create_and_run(
            store_id,
            source_type=payload.source_type,
            source_ref=payload.source_ref,
            range_start=payload.range_start,
            range_end=payload.range_end,
            event_ids=payload.event_ids,
            requested_by_user_id=access.user_id,
        )
    )
    return _job_to_out(job)


@router.get("/stores/{store_id}/replay", response_model=ReplayJobListResponse)
def list_replays(
    store_id: str, db: Session = Depends(get_db), access: StoreAccess = _read_access
) -> ReplayJobListResponse:
    jobs = ReplayService(db).list_jobs(store_id)
    return ReplayJobListResponse(store_id=store_id, jobs=[_job_to_out(job) for job in jobs])


@router.get("/stores/{store_id}/replay/{job_id}", response_model=ReplayJobOut)
def get_replay(
    store_id: str, job_id: str, db: Session = Depends(get_db), access: StoreAccess = _read_access
) -> ReplayJobOut:
    job = ReplayService(db).get_job(store_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Replay job not found.")
    return _job_to_out(job)


def _job_to_out(job: ReplayJob) -> ReplayJobOut:
    return ReplayJobOut(
        id=job.id,
        store_id=job.store_id,
        source_type=job.source_type,
        source_ref=job.source_ref,
        range_start=job.range_start,
        range_end=job.range_end,
        status=job.status,
        total_events=job.total_events,
        processed_events=job.processed_events,
        accepted_events=job.accepted_events,
        duplicate_events=job.duplicate_events,
        failed_events=job.failed_events,
        error_message=job.error_message,
        error_details=json.loads(job.error_details_json) if job.error_details_json else [],
        started_at=job.started_at,
        completed_at=job.completed_at,
        created_at=job.created_at,
    )
