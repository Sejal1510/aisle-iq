"""add video_processing_job and event/raw_event provenance columns

Revision ID: d4f9e2a8c1b3
Revises: c8a1f2b7d4e6
Create Date: 2026-09-19 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4f9e2a8c1b3'
down_revision: str | Sequence[str] | None = 'c8a1f2b7d4e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_STATUSES_SQL = "status IN ('PENDING', 'RUNNING')"


def upgrade() -> None:
    """P9: a VideoProcessingJob row tracks one video-processing run's camera/
    video selection, progress counters, and outcome -- the P9 sibling of
    ReplayJob (Operational Infrastructure), not a generalization of it (see
    the model's docstring). `event.video_processing_job_id` and
    `raw_event.video_processing_job_id` are purely additive and nullable, so
    every existing row is unaffected.

    The partial unique index on (camera_id) filtered to PENDING/RUNNING
    enforces "at most one active job per camera" at the database level, not
    just in application code -- this is what makes a duplicate "start
    processing" request for the same camera a safe, race-free no-op (see
    VideoProcessingService.create_job) rather than relying on a check-then-
    insert race window.
    """
    op.create_table('video_processing_job',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('store_id', sa.String(), nullable=False),
    sa.Column('camera_id', sa.String(), nullable=False),
    sa.Column('video_path', sa.String(), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED', name='videoprocessingstatus'), nullable=False),
    sa.Column('claimed_at', sa.DateTime(), nullable=True),
    sa.Column('total_events', sa.Integer(), nullable=False),
    sa.Column('processed_events', sa.Integer(), nullable=False),
    sa.Column('accepted_events', sa.Integer(), nullable=False),
    sa.Column('duplicate_events', sa.Integer(), nullable=False),
    sa.Column('failed_events', sa.Integer(), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('error_details_json', sa.Text(), nullable=True),
    sa.Column('requested_by_user_id', sa.String(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['requested_by_user_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['store_id'], ['store.id'], ),
    sa.ForeignKeyConstraint(['camera_id'], ['camera.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_video_processing_job_camera_id'), 'video_processing_job', ['camera_id'], unique=False)
    op.create_index(op.f('ix_video_processing_job_status'), 'video_processing_job', ['status'], unique=False)
    op.create_index(op.f('ix_video_processing_job_store_id'), 'video_processing_job', ['store_id'], unique=False)
    op.create_index(
        'ux_video_processing_job_camera_active',
        'video_processing_job',
        ['camera_id'],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_STATUSES_SQL),
        postgresql_where=sa.text(_ACTIVE_STATUSES_SQL),
    )

    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.add_column(sa.Column('video_processing_job_id', sa.String(), nullable=True))
        batch_op.create_index(batch_op.f('ix_event_video_processing_job_id'), ['video_processing_job_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_event_video_processing_job_id_video_processing_job',
            'video_processing_job', ['video_processing_job_id'], ['id']
        )

    with op.batch_alter_table('raw_event', schema=None) as batch_op:
        batch_op.add_column(sa.Column('video_processing_job_id', sa.String(), nullable=True))
        batch_op.create_index(batch_op.f('ix_raw_event_video_processing_job_id'), ['video_processing_job_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_raw_event_video_processing_job_id_video_processing_job',
            'video_processing_job', ['video_processing_job_id'], ['id']
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('raw_event', schema=None) as batch_op:
        batch_op.drop_constraint('fk_raw_event_video_processing_job_id_video_processing_job', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_raw_event_video_processing_job_id'))
        batch_op.drop_column('video_processing_job_id')

    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.drop_constraint('fk_event_video_processing_job_id_video_processing_job', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_event_video_processing_job_id'))
        batch_op.drop_column('video_processing_job_id')

    op.drop_index('ux_video_processing_job_camera_active', table_name='video_processing_job')
    op.drop_index(op.f('ix_video_processing_job_store_id'), table_name='video_processing_job')
    op.drop_index(op.f('ix_video_processing_job_status'), table_name='video_processing_job')
    op.drop_index(op.f('ix_video_processing_job_camera_id'), table_name='video_processing_job')
    op.drop_table('video_processing_job')

    # See replay's migration downgrade() for why this is needed on
    # PostgreSQL: op.drop_table only drops the table, not the native enum
    # TYPE its sa.Enum column used, which would otherwise break a
    # downgrade-then-upgrade cycle with "type already exists".
    bind = op.get_bind()
    sa.Enum(name='videoprocessingstatus').drop(bind, checkfirst=True)
