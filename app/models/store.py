from typing import List, Optional
from sqlalchemy import String, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from sqlalchemy.sql import func
import uuid

from app.db.base import Base
from app.models.enums import ZoneType

class Store(Base):
    __tablename__ = "store"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    cameras: Mapped[List["Camera"]] = relationship(back_populates="store", cascade="all, delete-orphan")
    zones: Mapped[List["Zone"]] = relationship(back_populates="store", cascade="all, delete-orphan")

class Camera(Base):
    __tablename__ = "camera"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    store: Mapped["Store"] = relationship(back_populates="cameras")

class Zone(Base):
    __tablename__ = "zone"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    type: Mapped[ZoneType] = mapped_column()
    is_revenue_zone: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    store: Mapped["Store"] = relationship(back_populates="zones")
