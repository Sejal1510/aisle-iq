import enum
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


class VideoProcessingStatus(str, enum.Enum):
    """Unlike ReplayStatus, this has a PENDING state: replay runs
    synchronously within the request that creates it (there is nothing to be
    "pending" about), but a video-processing job is created by the API and
    only starts once a separate worker process claims it -- see
    app/services/video_processing_service.py and app/worker/."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class VideoProcessingJob(Base):
    """Tracks one video-processing run: which camera/video it processed, its
    progress counters, and its outcome -- the P9 sibling of ReplayJob (P8
    Operational Infrastructure), not a generalization of it. The two job
    types share the same status/counter/error shape because their outcome
    semantics are identical (N candidate items -> accepted/duplicate/failed),
    but their *source* fields genuinely differ: a replay job's source is a
    (source_type, source_ref, time range) selection over already-parsed
    events, while a video-processing job's source is a single (camera_id,
    video_path) pair processed start-to-finish. Forcing those into one
    shared table would mean either overloading fields with dual meanings or
    carrying several always-null columns on every row -- kept as two plain
    tables instead, per the approved P9 specification.
    """

    __tablename__ = "video_processing_job"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("camera.id"), index=True)
    video_path: Mapped[str] = mapped_column(String)
    status: Mapped[VideoProcessingStatus] = mapped_column(index=True)
    claimed_at: Mapped[datetime | None] = mapped_column()
    total_events: Mapped[int] = mapped_column(Integer, default=0)
    processed_events: Mapped[int] = mapped_column(Integer, default=0)
    accepted_events: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_events: Mapped[int] = mapped_column(Integer, default=0)
    failed_events: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    error_details_json: Mapped[str | None] = mapped_column(Text)
    requested_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("user.id"))
    started_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    store: Mapped["Store"] = relationship()
    camera: Mapped["Camera"] = relationship()
