from collections.abc import Generator
from sqlalchemy import create_engine
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

def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency to yield a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
