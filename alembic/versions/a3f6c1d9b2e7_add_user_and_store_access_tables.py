"""add user and store_access tables

Revision ID: a3f6c1d9b2e7
Revises: 853b1b696474
Create Date: 2026-08-21 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3f6c1d9b2e7'
down_revision: str | Sequence[str] | None = '853b1b696474'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema. P4.4: adds User (human accounts) and StoreAccess
    (per-store role grants) -- purely additive, no changes to any existing
    table."""
    op.create_table('user',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('email', sa.String(), nullable=False),
    sa.Column('password_hash', sa.String(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_user_email'), 'user', ['email'], unique=True)
    op.create_table('store_access',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('user_id', sa.String(), nullable=False),
    sa.Column('store_id', sa.String(), nullable=False),
    sa.Column('role', sa.Enum('ADMIN', 'MANAGER', 'ANALYST', name='role'), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['store_id'], ['store.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'store_id', name='uq_store_access_user_store')
    )
    op.create_index(op.f('ix_store_access_store_id'), 'store_access', ['store_id'], unique=False)
    op.create_index(op.f('ix_store_access_user_id'), 'store_access', ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_store_access_user_id'), table_name='store_access')
    op.drop_index(op.f('ix_store_access_store_id'), table_name='store_access')
    op.drop_table('store_access')
    op.drop_index(op.f('ix_user_email'), table_name='user')
    op.drop_table('user')
