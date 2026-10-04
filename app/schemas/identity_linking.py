from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, model_validator

from app.models.enums import IdentityLinkReason
from app.schemas.spatial import SafeIdentifier


class CameraAdjacencyCreateRequest(BaseModel):
    from_camera_id: SafeIdentifier
    to_camera_id: SafeIdentifier
    min_transit_seconds: float
    max_transit_seconds: float

    @model_validator(mode="after")
    def _validate_window(self) -> CameraAdjacencyCreateRequest:
        if self.from_camera_id == self.to_camera_id:
            raise ValueError("from_camera_id and to_camera_id must be different cameras.")
        if self.min_transit_seconds < 0:
            raise ValueError("min_transit_seconds must be >= 0.")
        if self.max_transit_seconds < self.min_transit_seconds:
            raise ValueError("max_transit_seconds must be >= min_transit_seconds.")
        return self


class CameraAdjacencyOut(BaseModel):
    adjacency_id: str
    store_id: str
    from_camera_id: str
    to_camera_id: str
    min_transit_seconds: float
    max_transit_seconds: float
    created_at: datetime


class IdentityLinkCandidateOut(BaseModel):
    candidate_id: str
    store_id: str
    adjacency_id: str
    entity_a_id: str
    entity_b_id: str
    gap_seconds: float
    confidence: float
    accepted: bool
    reason: IdentityLinkReason
    evaluated_at: datetime


class IdentityLinkEvaluationResponse(BaseModel):
    store_id: str
    accepted: int
    ambiguous: int
    below_threshold: int
    total: int
