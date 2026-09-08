from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""
    pass


def utcnow() -> datetime:
    """Python-side UTC timestamp for ``created_at``/``received_at`` defaults.

    Deliberately not ``default=func.now()``: SQLAlchemy renders a SQL function
    default as a literal expression evaluated by the database itself, and
    SQLite's ``now()`` (CURRENT_TIMESTAMP) and PostgreSQL's ``now()`` don't
    agree on what "now" means -- SQLite's is always UTC, PostgreSQL's reflects
    the connection session's configured timezone, which is not guaranteed to
    be UTC. Evaluating in Python keeps these columns correct regardless of how
    any given PostgreSQL host is configured.
    """
    return datetime.now(UTC).replace(tzinfo=None)
