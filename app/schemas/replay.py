from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.replay import ReplaySourceType, ReplayStatus
from app.schemas.common import UtcDatetime


class ReplayCreateRequest(BaseModel):
    source_type: ReplaySourceType = ReplaySourceType.RAW_EVENT_ARCHIVE
    source_ref: str | None = None
    range_start: UtcDatetime | None = None
    range_end: UtcDatetime | None = None
    event_ids: list[str] | None = Field(
        default=None,
        description="Optional explicit set of RawEvent ids to replay (raw_event_archive source only). "
        "Additive with range_start/range_end when both are given.",
    )


class ReplayJobOut(BaseModel):
    id: str
    store_id: str
    source_type: ReplaySourceType
    source_ref: str | None
    range_start: UtcDatetime | None
    range_end: UtcDatetime | None
    status: ReplayStatus
    total_events: int
    processed_events: int
    accepted_events: int
    duplicate_events: int
    failed_events: int
    error_message: str | None
    error_details: list[dict] = Field(default_factory=list)
    started_at: UtcDatetime
    completed_at: UtcDatetime | None
    created_at: UtcDatetime


class ReplayJobListResponse(BaseModel):
    store_id: str
    jobs: list[ReplayJobOut]
