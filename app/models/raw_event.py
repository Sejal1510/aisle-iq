import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


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

    ``store_id`` (replay workflow) is denormalized from the payload rather
    than resolved only via ``event_id`` -- a raw archive row must stay
    queryable by store even if its canonical ``Event`` was later deleted
    (e.g. by exactly the kind of cleanup/bug that replaying the archive is
    meant to recover from); relying on a join through ``event_id`` would make
    that row unreachable at the moment it matters most. Nullable because it
    did not exist before the replay workflow -- rows written before this
    column existed are simply not scoped to a store for replay purposes.
    """

    __tablename__ = "raw_event"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    source: Mapped[str] = mapped_column(String, index=True)
    source_event_id: Mapped[str | None] = mapped_column(String, index=True)
    store_id: Mapped[str | None] = mapped_column(ForeignKey("store.id"), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("event.id"), index=True)
    validation_status: Mapped[str] = mapped_column(String, default="accepted", index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    replay_job_id: Mapped[str | None] = mapped_column(ForeignKey("replay_job.id"), index=True)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)

    event: Mapped[Optional["Event"]] = relationship()
