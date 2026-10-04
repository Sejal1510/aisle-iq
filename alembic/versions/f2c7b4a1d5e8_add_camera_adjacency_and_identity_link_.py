"""add camera_adjacency and identity_link_candidate

Revision ID: f2c7b4a1d5e8
Revises: d4f9e2a8c1b3
Create Date: 2026-09-28 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f2c7b4a1d5e8'
down_revision: str | Sequence[str] | None = 'd4f9e2a8c1b3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """P10.1: proposal-only cross-camera identity-link candidate generation.

    CameraAdjacency is purely operator-authored store configuration (which
    camera pairs are physically connected, and the transit-time window
    between them) -- the same nature as CameraCoverage, added in its own
    prior migration.

    IdentityLinkCandidate is an auditable, append-only evidence table: every
    pairwise candidate IdentityLinkingService.evaluate_candidates considers
    (accepted or rejected, with an explicit reason) is persisted here.
    Neither table is read by, or changes the behavior of, any existing
    table, model, or service -- both are purely additive, and no existing
    column changes.
    """
    op.create_table(
        'camera_adjacency',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('store_id', sa.String(), nullable=False),
        sa.Column('from_camera_id', sa.String(), nullable=False),
        sa.Column('to_camera_id', sa.String(), nullable=False),
        sa.Column('min_transit_seconds', sa.Float(), nullable=False),
        sa.Column('max_transit_seconds', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['store_id'], ['store.id'], ),
        sa.ForeignKeyConstraint(['from_camera_id'], ['camera.id'], ),
        sa.ForeignKeyConstraint(['to_camera_id'], ['camera.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('store_id', 'from_camera_id', 'to_camera_id', name='uq_camera_adjacency_pair'),
    )
    op.create_index(op.f('ix_camera_adjacency_store_id'), 'camera_adjacency', ['store_id'], unique=False)
    op.create_index(op.f('ix_camera_adjacency_from_camera_id'), 'camera_adjacency', ['from_camera_id'], unique=False)
    op.create_index(op.f('ix_camera_adjacency_to_camera_id'), 'camera_adjacency', ['to_camera_id'], unique=False)

    op.create_table(
        'identity_link_candidate',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('store_id', sa.String(), nullable=False),
        sa.Column('adjacency_id', sa.String(), nullable=False),
        sa.Column('entity_a_id', sa.String(), nullable=False),
        sa.Column('entity_b_id', sa.String(), nullable=False),
        sa.Column('gap_seconds', sa.Float(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('accepted', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.Enum('ACCEPTED', 'AMBIGUOUS', 'BELOW_THRESHOLD', name='identitylinkreason'), nullable=False),
        sa.Column('evaluated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['store_id'], ['store.id'], ),
        sa.ForeignKeyConstraint(['adjacency_id'], ['camera_adjacency.id'], ),
        sa.ForeignKeyConstraint(['entity_a_id'], ['tracked_entity.id'], ),
        sa.ForeignKeyConstraint(['entity_b_id'], ['tracked_entity.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'store_id', 'entity_a_id', 'entity_b_id', 'adjacency_id', name='uq_identity_link_candidate_pair'
        ),
    )
    op.create_index(op.f('ix_identity_link_candidate_store_id'), 'identity_link_candidate', ['store_id'], unique=False)
    op.create_index(
        op.f('ix_identity_link_candidate_adjacency_id'), 'identity_link_candidate', ['adjacency_id'], unique=False
    )
    op.create_index(
        op.f('ix_identity_link_candidate_entity_a_id'), 'identity_link_candidate', ['entity_a_id'], unique=False
    )
    op.create_index(
        op.f('ix_identity_link_candidate_entity_b_id'), 'identity_link_candidate', ['entity_b_id'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_identity_link_candidate_entity_b_id'), table_name='identity_link_candidate')
    op.drop_index(op.f('ix_identity_link_candidate_entity_a_id'), table_name='identity_link_candidate')
    op.drop_index(op.f('ix_identity_link_candidate_adjacency_id'), table_name='identity_link_candidate')
    op.drop_index(op.f('ix_identity_link_candidate_store_id'), table_name='identity_link_candidate')
    op.drop_table('identity_link_candidate')

    op.drop_index(op.f('ix_camera_adjacency_to_camera_id'), table_name='camera_adjacency')
    op.drop_index(op.f('ix_camera_adjacency_from_camera_id'), table_name='camera_adjacency')
    op.drop_index(op.f('ix_camera_adjacency_store_id'), table_name='camera_adjacency')
    op.drop_table('camera_adjacency')

    # See replay's/video-processing's migration downgrade() for why this is
    # needed on PostgreSQL: op.drop_table only drops the table, not the
    # native enum TYPE its sa.Enum column used, which would otherwise break
    # a downgrade-then-upgrade cycle with "type already exists".
    bind = op.get_bind()
    sa.Enum(name='identitylinkreason').drop(bind, checkfirst=True)
