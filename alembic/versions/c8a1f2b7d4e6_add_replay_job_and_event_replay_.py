"""add replay_job and event/raw_event replay tracking columns

Revision ID: c8a1f2b7d4e6
Revises: 79382dd38824
Create Date: 2026-09-12 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c8a1f2b7d4e6'
down_revision: str | Sequence[str] | None = '79382dd38824'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Raw-event replay workflow: a ReplayJob row tracks one replay run's
    source/range selection, progress counters, and outcome. `event.is_replay`
    / `event.replay_job_id` and `raw_event.store_id` / `raw_event.replay_job_id`
    are purely additive and nullable/defaulted, so every existing row is
    unaffected (is_replay defaults false -- all pre-existing events are,
    correctly, not replays; existing raw_event rows get a NULL store_id,
    meaning they predate store-scoped replay and are simply not part of any
    replay's candidate set)."""
    op.create_table('replay_job',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('store_id', sa.String(), nullable=False),
    sa.Column('source_type', sa.Enum('RAW_EVENT_ARCHIVE', 'JSONL_FILE', name='replaysourcetype'), nullable=False),
    sa.Column('source_ref', sa.String(), nullable=True),
    sa.Column('range_start', sa.DateTime(), nullable=True),
    sa.Column('range_end', sa.DateTime(), nullable=True),
    sa.Column('event_ids_json', sa.Text(), nullable=True),
    sa.Column('status', sa.Enum('RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED', name='replaystatus'), nullable=False),
    sa.Column('total_events', sa.Integer(), nullable=False),
    sa.Column('processed_events', sa.Integer(), nullable=False),
    sa.Column('accepted_events', sa.Integer(), nullable=False),
    sa.Column('duplicate_events', sa.Integer(), nullable=False),
    sa.Column('failed_events', sa.Integer(), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('error_details_json', sa.Text(), nullable=True),
    sa.Column('requested_by_user_id', sa.String(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['requested_by_user_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['store_id'], ['store.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_replay_job_source_type'), 'replay_job', ['source_type'], unique=False)
    op.create_index(op.f('ix_replay_job_status'), 'replay_job', ['status'], unique=False)
    op.create_index(op.f('ix_replay_job_store_id'), 'replay_job', ['store_id'], unique=False)

    # batch_alter_table (rather than plain op.add_column + op.create_foreign_key)
    # deliberately -- SQLite has no ALTER TABLE ADD CONSTRAINT, so adding a
    # foreign-keyed column to an *existing* table needs the copy-and-move
    # batch strategy there (unlike the tables created fresh above, where the
    # FK is just part of CREATE TABLE). This runs as a plain ALTER on
    # PostgreSQL. Constraint names are explicit (this project's metadata has
    # no naming_convention configured) so downgrade can reference them.
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_replay', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('replay_job_id', sa.String(), nullable=True))
        batch_op.create_index(batch_op.f('ix_event_replay_job_id'), ['replay_job_id'], unique=False)
        batch_op.create_foreign_key('fk_event_replay_job_id_replay_job', 'replay_job', ['replay_job_id'], ['id'])

    with op.batch_alter_table('raw_event', schema=None) as batch_op:
        # store_id is denormalized onto raw_event (not resolved only via a
        # join through event_id) so an archived row stays queryable by store
        # even if its canonical Event was later deleted -- see RawEvent's
        # docstring.
        batch_op.add_column(sa.Column('store_id', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('replay_job_id', sa.String(), nullable=True))
        batch_op.create_index(batch_op.f('ix_raw_event_store_id'), ['store_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_raw_event_replay_job_id'), ['replay_job_id'], unique=False)
        batch_op.create_foreign_key('fk_raw_event_store_id_store', 'store', ['store_id'], ['id'])
        batch_op.create_foreign_key(
            'fk_raw_event_replay_job_id_replay_job', 'replay_job', ['replay_job_id'], ['id']
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('raw_event', schema=None) as batch_op:
        batch_op.drop_constraint('fk_raw_event_replay_job_id_replay_job', type_='foreignkey')
        batch_op.drop_constraint('fk_raw_event_store_id_store', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_raw_event_replay_job_id'))
        batch_op.drop_index(batch_op.f('ix_raw_event_store_id'))
        batch_op.drop_column('replay_job_id')
        batch_op.drop_column('store_id')

    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.drop_constraint('fk_event_replay_job_id_replay_job', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_event_replay_job_id'))
        batch_op.drop_column('replay_job_id')
        batch_op.drop_column('is_replay')

    op.drop_index(op.f('ix_replay_job_store_id'), table_name='replay_job')
    op.drop_index(op.f('ix_replay_job_status'), table_name='replay_job')
    op.drop_index(op.f('ix_replay_job_source_type'), table_name='replay_job')
    op.drop_table('replay_job')

    # See the initial migration's downgrade() for why this is needed on
    # PostgreSQL: op.drop_table only drops the table, not the native enum
    # TYPE its sa.Enum columns used, which would otherwise break a
    # downgrade-then-upgrade cycle with "type already exists".
    bind = op.get_bind()
    sa.Enum(name='replaystatus').drop(bind, checkfirst=True)
    sa.Enum(name='replaysourcetype').drop(bind, checkfirst=True)
