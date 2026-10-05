from __future__ import annotations

from pydantic import BaseModel

from app.models.video_processing import VideoProcessingStatus
from app.schemas.common import UtcDatetime


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
    claimed_at: UtcDatetime | None
    started_at: UtcDatetime | None
    completed_at: UtcDatetime | None
    created_at: UtcDatetime
    # Annotated demo video (YOLO boxes + ByteTrack ids) produced by the run,
    # streamed from .../video-processing/{job_id}/annotated-video.
    annotated_video_available: bool = False
    run_summary: VideoRunSummaryOut | None = None


class VideoRunSummaryOut(BaseModel):
    """Tracker statistics counted during the run (never estimated)."""

    frames_sampled: int
    person_detections: int
    unique_tracks: int
    max_people_in_frame: int
    video_duration_seconds: float | None = None
    sample_fps: float | None = None
    annotation_error: str | None = None


class VideoProcessingJobListResponse(BaseModel):
    store_id: str
    jobs: list[VideoProcessingJobOut]
