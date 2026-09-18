import enum
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


class ReplaySourceType(str, enum.Enum):
    """Where a replay job reads its raw events from.

    ``RAW_EVENT_ARCHIVE`` replays rows already sitting in ``RawEvent`` (the
    archive P2.3 introduced) -- this is the primary, documented-as-roadmap
    workflow: reprocess history already ingested. ``JSONL_FILE`` replays a
    named dataset file under ``data/`` -- the API-exposed equivalent of
    ``pipeline/ingest_events.py``'s offline importer, for datasets that were
    never submitted through the live ingestion endpoints in the first place.
    """

    RAW_EVENT_ARCHIVE = "raw_event_archive"
    JSONL_FILE = "jsonl_file"


class ReplayStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class ReplayJob(Base):
    """Tracks one replay run: its source/range selection, progress counters,
    and outcome. Runs synchronously within the request that creates it (this
    project has no background task runner -- see app/services/replay_service.py)
    but is still persisted as a row so status/progress/errors can be queried
    after the fact, and so every ``Event``/``RawEvent`` row a replay touches
    can point back to the job that (re)produced it."""

    __tablename__ = "replay_job"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    source_type: Mapped[ReplaySourceType] = mapped_column(index=True)
    source_ref: Mapped[str | None] = mapped_column(String)
    range_start: Mapped[datetime | None] = mapped_column()
    range_end: Mapped[datetime | None] = mapped_column()
    event_ids_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ReplayStatus] = mapped_column(index=True)
    total_events: Mapped[int] = mapped_column(Integer, default=0)
    processed_events: Mapped[int] = mapped_column(Integer, default=0)
    accepted_events: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_events: Mapped[int] = mapped_column(Integer, default=0)
    failed_events: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    error_details_json: Mapped[str | None] = mapped_column(Text)
    requested_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("user.id"))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    store: Mapped["Store"] = relationship()
