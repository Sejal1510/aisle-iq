from collections.abc import Generator
from pathlib import Path

import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.base import Base

logger = structlog.get_logger(__name__)
settings = get_settings()

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Ensure SQLite can be used safely across threads in FastAPI
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    echo=settings.debug,  # Echo SQL queries to the logs if in debug mode
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

def init_db() -> None:
    """Ensure the database schema is ready to serve requests.

    development: bootstrap via ``create_all`` (creates whatever tables are
    missing -- typically all of them, for a brand-new local SQLite file).
    This is a zero-friction start for local work, not a migration tool: it
    cannot evolve an existing table (add/rename/drop a column). A dev
    database created before a schema change should be deleted and
    recreated, or brought up to date with ``alembic upgrade head`` --
    see docs/EVENT_CONTRACT.md's compatibility notes for why there isn't a
    safe automatic path for the former (no real data has ever lived in it).

    every other environment: verify the schema is already at the Alembic
    head revision and fail loudly and specifically if it is not, rather than
    silently starting against a stale or empty schema. Schema changes in
    these environments are applied by running ``alembic upgrade head`` as an
    explicit deploy step, not by the application at startup.
    """
    if settings.environment == "development":
        logger.info("ensuring_database_schema", database=settings.database_url, environment=settings.environment)
        Base.metadata.create_all(bind=engine)
        return

    logger.info("verifying_database_schema", database=settings.database_url, environment=settings.environment)
    _verify_schema_is_current()


def _verify_schema_is_current() -> None:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    script = ScriptDirectory.from_config(alembic_cfg)
    head_revision = script.get_current_head()

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        current_revision = context.get_current_revision()

    if current_revision != head_revision:
        raise RuntimeError(
            "Database schema is not up to date "
            f"(at revision {current_revision!r}, expected {head_revision!r}). "
            "Run `alembic upgrade head` before starting the app in this environment."
        )


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency to yield a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
