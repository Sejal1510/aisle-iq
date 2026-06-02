from typing import List, Optional
from sqlalchemy import String, Integer, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from sqlalchemy.sql import func
import uuid

from app.db.base import Base
from app.models.enums import SessionStatus

class TrackedEntity(Base):
    __tablename__ = "tracked_entity"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    gender: Mapped[Optional[str]] = mapped_column(String)
    age: Mapped[Optional[int]] = mapped_column(Integer)
    age_bucket: Mapped[Optional[str]] = mapped_column(String)
    is_staff: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)
    group_id: Mapped[Optional[str]] = mapped_column(String)
    group_size: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    sessions: Mapped[List["VisitSession"]] = relationship(back_populates="tracked_entity", cascade="all, delete-orphan")
    events: Mapped[List["Event"]] = relationship(back_populates="tracked_entity", cascade="all, delete-orphan")

class VisitSession(Base):
    __tablename__ = "visit_session"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tracked_entity_id: Mapped[str] = mapped_column(ForeignKey("tracked_entity.id"), index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    entry_time: Mapped[datetime] = mapped_column()
    exit_time: Mapped[Optional[datetime]] = mapped_column()
    dwell_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    session_status: Mapped[SessionStatus] = mapped_column(default=SessionStatus.IN_PROGRESS)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    tracked_entity: Mapped["TrackedEntity"] = relationship(back_populates="sessions")
    events: Mapped[List["Event"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    correlations: Mapped[List["TransactionCorrelation"]] = relationship(back_populates="session", cascade="all, delete-orphan")
