# PROMPT: Regression coverage for F-03 (P1) and its P2.5 completion -- database
# schema readiness must never be silently skipped or silently wrong outside
# development.
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

import app.db.session as session_module
from alembic import command
from app.models import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def scratch_sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'scratch.db'}"


def _alembic_config_for(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_init_db_bootstraps_in_development(monkeypatch: pytest.MonkeyPatch, scratch_sqlite_url: str) -> None:
    """Development keeps its zero-friction create_all bootstrap -- no Alembic
    step required to start working locally on a fresh database."""
    engine = create_engine(scratch_sqlite_url)
    monkeypatch.setattr(session_module, "engine", engine)
    monkeypatch.setattr(session_module.settings, "environment", "development")
    monkeypatch.setattr(session_module.settings, "database_url", scratch_sqlite_url)

    session_module.init_db()

    table_names = set(inspect(engine).get_table_names())
    expected_tables = set(Base.metadata.tables.keys())
    assert expected_tables, "no models registered on Base -- test fixture is broken"
    assert expected_tables.issubset(table_names)


def test_init_db_is_idempotent_in_development(monkeypatch: pytest.MonkeyPatch, scratch_sqlite_url: str) -> None:
    engine = create_engine(scratch_sqlite_url)
    monkeypatch.setattr(session_module, "engine", engine)
    monkeypatch.setattr(session_module.settings, "environment", "development")
    monkeypatch.setattr(session_module.settings, "database_url", scratch_sqlite_url)

    session_module.init_db()
    session_module.init_db()

    table_names = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables.keys()).issubset(table_names)


def test_init_db_verifies_schema_in_production_after_migration(
    monkeypatch: pytest.MonkeyPatch, scratch_sqlite_url: str
) -> None:
    """F-03/P2.5: a non-development environment whose database has actually
    been migrated to head starts cleanly -- init_db() only verifies, it does
    not create_all() or otherwise mutate schema outside development."""
    command.upgrade(_alembic_config_for(scratch_sqlite_url), "head")

    engine = create_engine(scratch_sqlite_url)
    monkeypatch.setattr(session_module, "engine", engine)
    monkeypatch.setattr(session_module.settings, "environment", "production")
    monkeypatch.setattr(session_module.settings, "database_url", scratch_sqlite_url)

    session_module.init_db()  # must not raise

    table_names = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables.keys()).issubset(table_names)


def test_init_db_fails_loudly_when_schema_not_migrated(
    monkeypatch: pytest.MonkeyPatch, scratch_sqlite_url: str
) -> None:
    """F-03 regression, completed: a non-development environment must never
    silently start against an empty/unmigrated schema. Previously this was a
    silent no-op that let the app boot and then fail confusingly on the first
    query; now it fails immediately at startup with an actionable message."""
    engine = create_engine(scratch_sqlite_url)  # never migrated -- no tables at all
    monkeypatch.setattr(session_module, "engine", engine)
    monkeypatch.setattr(session_module.settings, "environment", "production")
    monkeypatch.setattr(session_module.settings, "database_url", scratch_sqlite_url)

    with pytest.raises(RuntimeError, match="not up to date"):
        session_module.init_db()

    assert inspect(engine).get_table_names() == []


def test_init_db_fails_loudly_when_schema_is_stale(
    monkeypatch: pytest.MonkeyPatch, scratch_sqlite_url: str
) -> None:
    """A database migrated to an older revision (not head) is treated the same
    as an unmigrated one -- init_db() must not assume 'has some tables' means
    'has the right tables'."""
    config = _alembic_config_for(scratch_sqlite_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")  # leaves alembic_version but no app tables

    engine = create_engine(scratch_sqlite_url)
    monkeypatch.setattr(session_module, "engine", engine)
    monkeypatch.setattr(session_module.settings, "environment", "production")
    monkeypatch.setattr(session_module.settings, "database_url", scratch_sqlite_url)

    with pytest.raises(RuntimeError, match="not up to date"):
        session_module.init_db()
