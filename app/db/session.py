from collections.abc import Generator
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
import structlog

from app.core.config import get_settings
from app.db.base import Base

logger = structlog.get_logger(__name__)
settings = get_settings()

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
    """Create all database tables. Useful for development bootstrapping."""
    if settings.environment == "development":
        logger.info("creating_database_tables", database=settings.database_url)
        Base.metadata.create_all(bind=engine)
        _ensure_development_sqlite_columns()


def _ensure_development_sqlite_columns() -> None:
    """Add nullable columns needed by newer models to an existing dev SQLite DB."""
    if not settings.database_url.startswith("sqlite"):
        return

    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    if "event" in table_names:
        _ensure_event_columns(inspector)

    if "tracked_entity" in table_names:
        _ensure_tracked_entity_columns(inspector)

    if "transaction_correlation" in table_names:
        _ensure_correlation_columns(inspector)


def _ensure_event_columns(inspector) -> None:
    existing_columns = {column["name"] for column in inspector.get_columns("event")}
    required_columns = {
        "source_event_id": "VARCHAR",
        "confidence": "FLOAT",
        "metadata_json": "TEXT",
        "queue_event_id": "VARCHAR",
        "queue_join_ts": "DATETIME",
        "queue_served_ts": "DATETIME",
        "queue_exit_ts": "DATETIME",
        "wait_seconds": "INTEGER",
        "queue_position_at_join": "INTEGER",
        "abandoned": "BOOLEAN",
    }
    missing_columns = {
        column_name: column_type
        for column_name, column_type in required_columns.items()
        if column_name not in existing_columns
    }

    if not missing_columns:
        return

    with engine.begin() as connection:
        for column_name, column_type in missing_columns.items():
            logger.info("adding_development_sqlite_column", table="event", column=column_name)
            connection.execute(text(f"ALTER TABLE event ADD COLUMN {column_name} {column_type}"))


def _ensure_tracked_entity_columns(inspector) -> None:
    existing_columns = {column["name"] for column in inspector.get_columns("tracked_entity")}
    required_columns = {
        "staff_confidence_score": "FLOAT DEFAULT 0.0",
        "staff_inference_reason": "VARCHAR",
    }
    missing_columns = {
        column_name: column_type
        for column_name, column_type in required_columns.items()
        if column_name not in existing_columns
    }

    if not missing_columns:
        return

    with engine.begin() as connection:
        for column_name, column_type in missing_columns.items():
            logger.info("adding_development_sqlite_column", table="tracked_entity", column=column_name)
            connection.execute(text(f"ALTER TABLE tracked_entity ADD COLUMN {column_name} {column_type}"))


def _ensure_correlation_columns(inspector) -> None:
    columns = inspector.get_columns("transaction_correlation")
    existing_columns = {column["name"] for column in columns}
    required_columns = {
        "status": "VARCHAR DEFAULT 'matched'",
        "explanation": "TEXT",
    }
    missing_columns = {
        column_name: column_type
        for column_name, column_type in required_columns.items()
        if column_name not in existing_columns
    }

    if missing_columns:
        with engine.begin() as connection:
            for column_name, column_type in missing_columns.items():
                logger.info("adding_development_sqlite_column", table="transaction_correlation", column=column_name)
                connection.execute(text(f"ALTER TABLE transaction_correlation ADD COLUMN {column_name} {column_type}"))

    refreshed_columns = inspector.get_columns("transaction_correlation")
    requires_nullable_rebuild = any(
        column["name"] in {"transaction_id", "session_id"} and not column["nullable"]
        for column in refreshed_columns
    )
    if requires_nullable_rebuild:
        _rebuild_correlation_table_with_nullable_links()


def _rebuild_correlation_table_with_nullable_links() -> None:
    logger.info("rebuilding_development_sqlite_table", table="transaction_correlation")
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(
            text(
                """
                CREATE TABLE transaction_correlation_new (
                    id VARCHAR NOT NULL PRIMARY KEY,
                    transaction_id VARCHAR REFERENCES pos_transaction (id),
                    session_id VARCHAR REFERENCES visit_session (id),
                    status VARCHAR NOT NULL DEFAULT 'matched',
                    confidence_score FLOAT NOT NULL,
                    correlation_method VARCHAR NOT NULL,
                    explanation TEXT,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO transaction_correlation_new (
                    id,
                    transaction_id,
                    session_id,
                    status,
                    confidence_score,
                    correlation_method,
                    explanation,
                    created_at
                )
                SELECT
                    id,
                    transaction_id,
                    session_id,
                    COALESCE(status, 'matched'),
                    confidence_score,
                    correlation_method,
                    explanation,
                    created_at
                FROM transaction_correlation
                """
            )
        )
        connection.execute(text("DROP TABLE transaction_correlation"))
        connection.execute(text("ALTER TABLE transaction_correlation_new RENAME TO transaction_correlation"))
        connection.execute(text("CREATE INDEX ix_transaction_correlation_transaction_id ON transaction_correlation (transaction_id)"))
        connection.execute(text("CREATE INDEX ix_transaction_correlation_session_id ON transaction_correlation (session_id)"))
        connection.execute(text("CREATE INDEX ix_transaction_correlation_status ON transaction_correlation (status)"))
        connection.execute(text("PRAGMA foreign_keys=ON"))

def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency to yield a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
