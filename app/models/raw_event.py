from typing import Optional
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from sqlalchemy.sql import func
import uuid

from app.db.base import Base


class RawEvent(Base):
    """The exact payload a caller submitted, preserved independently of the
    normalized ``Event`` row it produced.

    Why this exists: source event schemas already vary (id_token-based
    entry/exit, track_id-based zone/queue, the canonical video-pipeline shape)
    and will keep evolving as more ingestion sources are added. Persisting the
    raw payload -- not just what the current normalization logic extracted
    from it -- means a schema or normalization bug can be diagnosed and
    replayed against the original input instead of only against what
    ingestion already decided to keep.

    One row is written per ingestion attempt, including duplicates (a
    resubmitted event still gets its own RawEvent row even though it does not
    produce a second canonical Event) -- RawEvent is a receipt log, not a
    deduplicated one.
    """

    __tablename__ = "raw_event"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    source: Mapped[str] = mapped_column(String, index=True)
    source_event_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    event_id: Mapped[Optional[str]] = mapped_column(ForeignKey("event.id"), index=True)
    validation_status: Mapped[str] = mapped_column(String, default="accepted", index=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(default=func.now())

    event: Mapped[Optional["Event"]] = relationship()
