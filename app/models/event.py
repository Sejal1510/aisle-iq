from typing import Optional
from sqlalchemy import Boolean, Integer, String, Float, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from sqlalchemy.sql import func
import uuid

from app.db.base import Base
from app.models.enums import EventType

class Event(Base):
    """The idempotency key is (store_id, source_event_id), not source_event_id
    alone: a bare-global-unique source_event_id would let one store's event
    silently block a different store's event that happens to reuse the same
    source-provided id (e.g. two independent CCTV systems both starting an
    incrementing counter at 1). See EventIngestionService.process_event for
    the corresponding lookup."""

    __tablename__ = "event"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    source_event_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("visit_session.id"), index=True)
    tracked_entity_id: Mapped[str] = mapped_column(ForeignKey("tracked_entity.id"), index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    camera_id: Mapped[Optional[str]] = mapped_column(ForeignKey("camera.id"))
    zone_id: Mapped[Optional[str]] = mapped_column(ForeignKey("zone.id"))
    event_type: Mapped[EventType] = mapped_column(index=True)
    timestamp: Mapped[datetime] = mapped_column(index=True)
    hotspot_x: Mapped[Optional[float]] = mapped_column(Float)
    hotspot_y: Mapped[Optional[float]] = mapped_column(Float)
    is_face_hidden: Mapped[Optional[bool]] = mapped_column()
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text)
    queue_event_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    queue_join_ts: Mapped[Optional[datetime]] = mapped_column()
    queue_served_ts: Mapped[Optional[datetime]] = mapped_column()
    queue_exit_ts: Mapped[Optional[datetime]] = mapped_column()
    wait_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    queue_position_at_join: Mapped[Optional[int]] = mapped_column(Integer)
    abandoned: Mapped[Optional[bool]] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    session: Mapped["VisitSession"] = relationship(back_populates="events")
    tracked_entity: Mapped["TrackedEntity"] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint("store_id", "source_event_id", name="uq_event_store_source_event_id"),
    )
