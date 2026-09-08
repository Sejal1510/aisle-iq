# P6: PostgreSQL migration verification. The rest of the suite runs entirely
# on SQLite by design (see docs/CHOICES.md) -- these are the only tests that
# touch a real PostgreSQL instance, and they exist specifically to catch what
# a SQLite-only suite structurally cannot: whether the Alembic migrations
# themselves apply cleanly to PostgreSQL, including the native enum-type
# handling that has no SQLite equivalent.
#
# Opt-in via AISLEIQ_TEST_POSTGRES_URL -- skipped entirely otherwise. CI sets
# it against a postgres:16 service container (see .github/workflows/ci.yml);
# docker-compose.yml's `db` service is the equivalent for local runs, e.g.:
#   AISLEIQ_TEST_POSTGRES_URL=postgresql+psycopg://aisleiq:aisleiq@localhost:5432/aisleiq pytest tests/test_postgres_migration.py
import os
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from app.models import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POSTGRES_URL = os.environ.get("AISLEIQ_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="AISLEIQ_TEST_POSTGRES_URL not set -- PostgreSQL verification is opt-in",
)


def _alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", POSTGRES_URL)
    return config


@pytest.fixture()
def alembic_config() -> Config:
    """Each test starts from -- and leaves -- an empty schema. downgrade to
    'base' is a safe no-op against an already-empty database (including on
    the very first run, before alembic_version exists), so this doesn't
    assume anything about what earlier tests in this file left behind."""
    config = _alembic_config()
    command.downgrade(config, "base")
    yield config
    command.downgrade(config, "base")


def _table_names() -> set[str]:
    engine = create_engine(POSTGRES_URL)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_head_from_empty_database(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")

    expected_tables = set(Base.metadata.tables.keys())
    assert expected_tables, "no models registered on Base -- test fixture is broken"
    assert expected_tables.issubset(_table_names())


def test_upgrade_head_is_idempotent(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    command.upgrade(alembic_config, "head")  # must not raise

    assert set(Base.metadata.tables.keys()).issubset(_table_names())


def test_downgrade_then_upgrade_roundtrip(alembic_config: Config) -> None:
    """Regression coverage for the enum-type cleanup in both migrations'
    downgrade(): op.drop_table() does not drop the PostgreSQL native enum
    TYPE a dropped table's Enum column used, so without the explicit
    sa.Enum(...).drop() calls, this second upgrade() fails with
    'type "eventtype" already exists' (and similarly for the other four
    enum-backed columns)."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")  # must not raise

    assert set(Base.metadata.tables.keys()).issubset(_table_names())
    assert "alembic_version" in _table_names()


def test_downgrade_from_head_removes_all_app_tables(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    remaining = _table_names() & set(Base.metadata.tables.keys())
    assert remaining == set()
