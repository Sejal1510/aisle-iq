from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.models.video_processing import VideoProcessingStatus


class VideoProcessingJobCreateRequest(BaseModel):
    camera_id: str


class VideoProcessingJobOut(BaseModel):
    id: str
    store_id: str
    camera_id: str
    status: VideoProcessingStatus
    total_events: int
    processed_events: int
    accepted_events: int
    duplicate_events: int
    failed_events: int
    error_message: str | None
    error_details: dict | list | None = None
    claimed_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class VideoProcessingJobListResponse(BaseModel):
    store_id: str
    jobs: list[VideoProcessingJobOut]
