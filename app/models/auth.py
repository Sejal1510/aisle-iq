import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class ApiKey(Base):
    """A per-store credential used to authenticate event ingestion requests.

    Only the hash of the raw key is ever persisted (see ``app.core.security``); the
    raw value is returned to the caller exactly once, at creation time, and cannot
    be recovered afterward.
    """

    __tablename__ = "api_key"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
