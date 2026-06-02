from typing import Optional
from sqlalchemy import String, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from sqlalchemy.sql import func
import uuid

from app.db.base import Base
from app.models.enums import EventType

class Event(Base):
    __tablename__ = "event"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
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
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    session: Mapped["VisitSession"] = relationship(back_populates="events")
    tracked_entity: Mapped["TrackedEntity"] = relationship(back_populates="events")
