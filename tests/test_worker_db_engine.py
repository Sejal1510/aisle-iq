# P9: focused tests for app.db.worker_session -- the future worker's own
# engine construction (not wired into anything yet; there is no worker poll
# loop or job-claiming code in this change). Only the connect_args-selection
# logic and a basic "the engine actually works" sanity check are covered
# here; no artificial concurrency/locking stress test, per the audit's own
# instruction not to add one just for coverage.
from sqlalchemy import text

from app.db.worker_session import (
    DEFAULT_SQLITE_BUSY_TIMEOUT_SECONDS,
    create_worker_engine,
    sqlite_busy_timeout_connect_args,
)


def test_sqlite_url_gets_a_busy_timeout() -> None:
    connect_args = sqlite_busy_timeout_connect_args("sqlite:///./data/app.db", busy_timeout_seconds=5.0)
    assert connect_args == {"check_same_thread": False, "timeout": 5.0}


def test_sqlite_memory_url_gets_a_busy_timeout_too() -> None:
    connect_args = sqlite_busy_timeout_connect_args("sqlite:///:memory:", busy_timeout_seconds=2.5)
    assert connect_args == {"check_same_thread": False, "timeout": 2.5}


def test_postgres_url_gets_no_sqlite_specific_connect_args() -> None:
    connect_args = sqlite_busy_timeout_connect_args(
        "postgresql+psycopg://aisleiq:aisleiq@db:5432/aisleiq", busy_timeout_seconds=5.0
    )
    assert connect_args == {}


def test_create_worker_engine_uses_the_default_timeout_for_sqlite() -> None:
    engine = create_worker_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        assert connection.execute(text("select 1")).scalar() == 1
    engine.dispose()

    assert DEFAULT_SQLITE_BUSY_TIMEOUT_SECONDS > 0


def test_create_worker_engine_actually_connects_for_postgres_url_shape() -> None:
    """Doesn't connect to a real PostgreSQL server -- just verifies engine
    construction doesn't blow up on a postgres URL and that no sqlite-only
    connect_args leak into it (create_engine would reject an unknown kwarg
    for the psycopg driver if it did)."""
    engine = create_worker_engine("postgresql+psycopg://aisleiq:aisleiq@db:5432/aisleiq")
    assert engine.dialect.name == "postgresql"
    engine.dispose()
