"""add STAFF_AREA zone type

Revision ID: b7e3d9c2a614
Revises: f2c7b4a1d5e8
Create Date: 2026-10-05 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7e3d9c2a614'
down_revision: str | Sequence[str] | None = 'f2c7b4a1d5e8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ZoneType.STAFF_AREA: an operator-drawn staff-only area used to
    classify tracks as staff (see app.services.video_run_finalizer).

    PostgreSQL stores zone.type as the native ``zonetype`` enum, which needs
    the new value added explicitly. SQLite stores the column as plain text
    (non-native enum, no CHECK constraint), so there is nothing to change.
    """
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE zonetype ADD VALUE IF NOT EXISTS 'STAFF_AREA'")


def downgrade() -> None:
    """PostgreSQL cannot drop a value from an enum type in place. Zones of
    this type are re-labelled OTHER so the older code (which doesn't know
    STAFF_AREA) can still load them; the unused enum value is left behind,
    which is harmless."""
    op.execute("UPDATE zone SET type = 'OTHER' WHERE type = 'STAFF_AREA'")
