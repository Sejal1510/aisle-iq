from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import require_store_role
from app.db.session import get_db
from app.models.auth import StoreAccess
from app.models.enums import Role
from app.models.identity_linking import CameraAdjacency, IdentityLinkCandidate
from app.schemas.identity_linking import (
    CameraAdjacencyCreateRequest,
    CameraAdjacencyOut,
    IdentityLinkCandidateOut,
    IdentityLinkEvaluationResponse,
)
from app.services.identity_linking_service import (
    CandidateEvaluationSummary,
    IdentityLinkingService,
)
from app.services.store_config_service import StoreConfigError, StoreConfigService

router = APIRouter()

# Camera relationships are structural store configuration -- same ADMIN/
# ANALYST floor app.api.onboarding uses for Camera/Zone/CameraCoverage.
# Triggering an evaluation run is an operational action on already-processed
# footage -- MANAGER, the same floor app.api.video_processing uses for
# starting a processing job. Reviewing evidence stays at the ANALYST floor
# every other analytics/read route in this project uses.
_config_write_access = Depends(require_store_role(Role.ADMIN))
_config_read_access = Depends(require_store_role(Role.ANALYST))
_run_access = Depends(require_store_role(Role.MANAGER))
_read_access = Depends(require_store_role(Role.ANALYST))


def _run(callable_):
    try:
        return callable_()
    except StoreConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/stores/{store_id}/camera-adjacency", response_model=CameraAdjacencyOut, status_code=201)
def create_camera_adjacency(
    store_id: str,
    payload: CameraAdjacencyCreateRequest,
    db: Session = Depends(get_db),
    access: StoreAccess = _config_write_access,
) -> CameraAdjacencyOut:
    adjacency = _run(
        lambda: StoreConfigService(db).create_camera_adjacency(
            store_id,
            from_camera_id=payload.from_camera_id,
            to_camera_id=payload.to_camera_id,
            min_transit_seconds=payload.min_transit_seconds,
            max_transit_seconds=payload.max_transit_seconds,
        )
    )
    db.commit()
    return _adjacency_to_out(adjacency)


@router.get("/stores/{store_id}/camera-adjacency", response_model=list[CameraAdjacencyOut])
def list_camera_adjacency(
    store_id: str, db: Session = Depends(get_db), access: StoreAccess = _config_read_access
) -> list[CameraAdjacencyOut]:
    return [_adjacency_to_out(a) for a in StoreConfigService(db).list_camera_adjacency(store_id)]


@router.post("/stores/{store_id}/identity-link-candidates", response_model=IdentityLinkEvaluationResponse)
def evaluate_identity_link_candidates(
    store_id: str, db: Session = Depends(get_db), access: StoreAccess = _run_access
) -> IdentityLinkEvaluationResponse:
    """Evaluate every CameraAdjacency configured for this store and persist
    pairwise candidate evidence (accepted and rejected) -- see
    IdentityLinkingService's module docstring for what this does and, just
    as importantly, does not do (no merging, no canonical identity, no
    transitive resolution)."""
    candidates = IdentityLinkingService(db).evaluate_candidates(store_id)
    db.commit()
    return _summary_to_response(store_id, _summarize(candidates))


@router.get("/stores/{store_id}/identity-link-candidates", response_model=list[IdentityLinkCandidateOut])
def list_identity_link_candidates(
    store_id: str,
    accepted: bool | None = Query(default=None),
    db: Session = Depends(get_db),
    access: StoreAccess = _read_access,
) -> list[IdentityLinkCandidateOut]:
    query = select(IdentityLinkCandidate).where(IdentityLinkCandidate.store_id == store_id)
    if accepted is not None:
        query = query.where(IdentityLinkCandidate.accepted == accepted)
    query = query.order_by(IdentityLinkCandidate.evaluated_at.desc())
    return [_candidate_to_out(c) for c in db.scalars(query)]


def _summarize(candidates: list[IdentityLinkCandidate]) -> CandidateEvaluationSummary:
    accepted = sum(1 for c in candidates if c.accepted)
    ambiguous = sum(1 for c in candidates if not c.accepted and c.reason.value == "ambiguous")
    below_threshold = sum(1 for c in candidates if not c.accepted and c.reason.value == "below_threshold")
    return CandidateEvaluationSummary(accepted=accepted, ambiguous=ambiguous, below_threshold=below_threshold)


def _summary_to_response(store_id: str, summary: CandidateEvaluationSummary) -> IdentityLinkEvaluationResponse:
    return IdentityLinkEvaluationResponse(
        store_id=store_id,
        accepted=summary.accepted,
        ambiguous=summary.ambiguous,
        below_threshold=summary.below_threshold,
        total=summary.total,
    )


def _adjacency_to_out(adjacency: CameraAdjacency) -> CameraAdjacencyOut:
    return CameraAdjacencyOut(
        adjacency_id=adjacency.id,
        store_id=adjacency.store_id,
        from_camera_id=adjacency.from_camera_id,
        to_camera_id=adjacency.to_camera_id,
        min_transit_seconds=adjacency.min_transit_seconds,
        max_transit_seconds=adjacency.max_transit_seconds,
        created_at=adjacency.created_at,
    )


def _candidate_to_out(candidate: IdentityLinkCandidate) -> IdentityLinkCandidateOut:
    return IdentityLinkCandidateOut(
        candidate_id=candidate.id,
        store_id=candidate.store_id,
        adjacency_id=candidate.adjacency_id,
        entity_a_id=candidate.entity_a_id,
        entity_b_id=candidate.entity_b_id,
        gap_seconds=candidate.gap_seconds,
        confidence=candidate.confidence,
        accepted=candidate.accepted,
        reason=candidate.reason,
        evaluated_at=candidate.evaluated_at,
    )
