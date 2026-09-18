from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.replay import ReplaySourceType, ReplayStatus


class ReplayCreateRequest(BaseModel):
    source_type: ReplaySourceType = ReplaySourceType.RAW_EVENT_ARCHIVE
    source_ref: str | None = None
    range_start: datetime | None = None
    range_end: datetime | None = None
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
    range_start: datetime | None
    range_end: datetime | None
    status: ReplayStatus
    total_events: int
    processed_events: int
    accepted_events: int
    duplicate_events: int
    failed_events: int
    error_message: str | None
    error_details: list[dict] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime


class ReplayJobListResponse(BaseModel):
    store_id: str
    jobs: list[ReplayJobOut]
