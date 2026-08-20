from typing import List, Optional
from sqlalchemy import String, Integer, Boolean, ForeignKey, Float, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from sqlalchemy.sql import func
import uuid

from app.db.base import Base
from app.models.enums import SessionStatus


class TrackedEntity(Base):
    """A canonical visitor. ``id`` is a platform-generated UUID -- it is never a
    raw source identifier (id_token / track_id / visitor_id). Those are stored
    as ``IdentityAlias`` rows pointing back to this entity, which is what makes
    the id namespace-independent instead of the id_token==track_id assumption
    the original ingestion code made."""

    __tablename__ = "tracked_entity"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    gender: Mapped[Optional[str]] = mapped_column(String)
    age: Mapped[Optional[int]] = mapped_column(Integer)
    age_bucket: Mapped[Optional[str]] = mapped_column(String)
    is_staff: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)
    staff_confidence_score: Mapped[Optional[float]] = mapped_column(Float, default=0.0)
    staff_inference_reason: Mapped[Optional[str]] = mapped_column(String)
    group_id: Mapped[Optional[str]] = mapped_column(String)
    group_size: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    sessions: Mapped[List["VisitSession"]] = relationship(back_populates="tracked_entity", cascade="all, delete-orphan")
    events: Mapped[List["Event"]] = relationship(back_populates="tracked_entity", cascade="all, delete-orphan")
    aliases: Mapped[List["IdentityAlias"]] = relationship(back_populates="tracked_entity", cascade="all, delete-orphan")


class IdentityAlias(Base):
    """Maps one source-system identifier to a canonical TrackedEntity.

    ``camera_id`` is non-nullable with a sentinel value (see
    ``STORE_SCOPED_CAMERA_ID``) rather than SQL NULL for identifiers that are
    not camera-scoped (entry/exit id_token values, which are stable across a
    store's whole entry system) -- a NULL column does not participate in a
    UNIQUE constraint the way a real value does, on either SQLite or
    PostgreSQL, which would silently defeat the uniqueness this table exists
    to enforce.

    This table intentionally does not implement any cross-alias merging or
    matching logic (no ReID). Each distinct (store, camera scope, source
    field, source value) is deterministically resolved to exactly one
    TrackedEntity, the same behavior the P1 composite-string id had -- this
    phase changes the storage model, not the resolution behavior, so future
    identity-linking work has a real place to plug into.
    """

    __tablename__ = "identity_alias"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tracked_entity_id: Mapped[str] = mapped_column(ForeignKey("tracked_entity.id"), index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    camera_id: Mapped[str] = mapped_column(String)
    source_field: Mapped[str] = mapped_column(String)
    source_value: Mapped[str] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    first_seen_at: Mapped[datetime] = mapped_column()
    last_seen_at: Mapped[datetime] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    tracked_entity: Mapped["TrackedEntity"] = relationship(back_populates="aliases")

    __table_args__ = (
        UniqueConstraint("store_id", "camera_id", "source_field", "source_value", name="uq_identity_alias_scope"),
    )


STORE_SCOPED_CAMERA_ID = "__STORE_SCOPED__"
"""Sentinel ``IdentityAlias.camera_id`` for identifiers that are stable across
a store's whole entry system rather than local to one camera (id_token).
See the ``IdentityAlias`` docstring for why this is a non-null sentinel
instead of SQL NULL."""


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
