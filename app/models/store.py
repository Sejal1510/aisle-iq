from typing import List, Optional
from sqlalchemy import String, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from sqlalchemy.sql import func

from app.db.base import Base
from app.models.enums import ZoneType


class Organization(Base):
    """The tenant boundary above Store. There is exactly one row in practice
    today (single-tenant deployment) -- this table exists so the hierarchy is
    representable in the schema and Store rows carry a real foreign key,
    without building any multi-tenant business logic or API surface around it
    yet. See ReferenceDataService for how a default organization is
    provisioned."""

    __tablename__ = "organization"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    stores: Mapped[List["Store"]] = relationship(back_populates="organization")


class Store(Base):
    """``organization_id`` is nullable by design, not an oversight: ingestion
    (``ReferenceDataService.ensure_store``) always assigns a real
    organization, but a nullable column means the many existing call sites
    that construct a ``Store`` directly (tests, the POS ingestion path, demo
    fixtures) do not all have to become organization-aware in this phase just
    to keep working. A NULL organization_id is valid under both SQLite and
    PostgreSQL foreign key semantics."""

    __tablename__ = "store"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    organization_id: Mapped[Optional[str]] = mapped_column(ForeignKey("organization.id"), index=True)
    name: Mapped[Optional[str]] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    organization: Mapped["Organization"] = relationship(back_populates="stores")
    cameras: Mapped[List["Camera"]] = relationship(back_populates="store", cascade="all, delete-orphan")
    zones: Mapped[List["Zone"]] = relationship(back_populates="store", cascade="all, delete-orphan")


class Camera(Base):
    __tablename__ = "camera"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    name: Mapped[Optional[str]] = mapped_column(String)
    role: Mapped[Optional[str]] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    store: Mapped["Store"] = relationship(back_populates="cameras")


class Zone(Base):
    __tablename__ = "zone"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    type: Mapped[ZoneType] = mapped_column()
    is_revenue_zone: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    store: Mapped["Store"] = relationship(back_populates="zones")
