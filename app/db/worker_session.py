"""Database engine construction for the future P9 video-processing worker.

Not wired into anything yet -- the worker itself (poll loop, job claiming)
does not exist. This exists so that whenever it's built, it starts from a
SQLite busy_timeout that app.db.session's own engine deliberately does not
set (see its ``connect_args`` line): the worker's job-claim ``UPDATE``
(a single atomic ``UPDATE ... WHERE status='PENDING' ... RETURNING *`` --
see the P9 worker-readiness audit) will contend with the API process under
SQLite, which serializes *all* writers at the whole-database-file level.
Without a timeout, the losing writer gets ``sqlite3.OperationalError:
database is locked`` immediately instead of waiting briefly for the winner
to commit. PostgreSQL's locking is row-level, not file-level, so it needs no
equivalent setting -- this mirrors app.db.session's existing sqlite-only
``connect_args`` branch, just with a nonzero timeout.

Kept separate from app.db.session (rather than adding a timeout to its
existing ``connect_args``) because that engine is shared by every
request-scoped ``get_db()`` session in the running API -- widening its
lock-wait behavior there would change how the API itself behaves under
contention, for a problem that is specific to the worker's polling/claiming
pattern, not to request handling.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

DEFAULT_SQLITE_BUSY_TIMEOUT_SECONDS = 5.0


def sqlite_busy_timeout_connect_args(database_url: str, *, busy_timeout_seconds: float) -> dict:
    """The ``connect_args`` a worker engine should use for ``database_url``.

    A pure function (no engine construction) so the URL-branching logic is
    directly testable without needing a real database connection.
    """
    if not database_url.startswith("sqlite"):
        return {}
    return {"check_same_thread": False, "timeout": busy_timeout_seconds}


def create_worker_engine(
    database_url: str, *, busy_timeout_seconds: float = DEFAULT_SQLITE_BUSY_TIMEOUT_SECONDS
) -> Engine:
    """Build the SQLAlchemy engine the future worker process should use.

    Mirrors app.db.session's own ``create_engine`` call (same
    ``check_same_thread`` handling), adding only the busy_timeout described
    above.
    """
    return create_engine(
        database_url,
        connect_args=sqlite_busy_timeout_connect_args(database_url, busy_timeout_seconds=busy_timeout_seconds),
    )
