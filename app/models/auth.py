import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.models.enums import Role


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


class User(Base):
    """A human account (P4.4). Distinct from ApiKey: a person who logs into the
    dashboard, not a machine credential for ingestion. Passwords are hashed
    with bcrypt (a slow hash), unlike ApiKey's SHA-256 -- these are low-entropy,
    user-chosen secrets, not high-entropy generated tokens, so a slow hash is
    the correct tradeoff here (see app.core.security).

    There is no self-service signup in this phase: rows are created by the
    offline bootstrap script documented in README.md, mirroring how the first
    ApiKey is created."""

    __tablename__ = "user"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())


class StoreAccess(Base):
    """Grants one User a Role scoped to one Store. Store-scoped rather than
    Organization-scoped: every protected analytics route is already keyed by
    store_id, and Organization is currently a single-row stub with no
    operative logic of its own -- resolving permissions through it would add
    a layer this project doesn't use anywhere else yet."""

    __tablename__ = "store_access"
    __table_args__ = (UniqueConstraint("user_id", "store_id", name="uq_store_access_user_store"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    role: Mapped[Role] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=func.now())
