"""add map, camera_coverage, and camera/zone spatial config columns

Revision ID: 79382dd38824
Revises: a3f6c1d9b2e7
Create Date: 2026-09-05 01:04:00.274869

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '79382dd38824'
down_revision: str | Sequence[str] | None = 'a3f6c1d9b2e7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema. P7: adds the store spatial-configuration layer --
    Map (uploaded floor plan asset), CameraCoverage (camera-frame <-> zone
    geometry), and the per-camera video-processing columns plus per-zone
    map geometry that used to live only in pipeline/video/config.py Python
    literals. Purely additive, no changes to any existing column/table."""
    op.create_table('map',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('store_id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=True),
    sa.Column('file_path', sa.String(), nullable=False),
    sa.Column('content_type', sa.String(), nullable=True),
    sa.Column('width_px', sa.Integer(), nullable=True),
    sa.Column('height_px', sa.Integer(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['store_id'], ['store.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_map_store_id'), 'map', ['store_id'], unique=False)
    op.create_table('camera_coverage',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('camera_id', sa.String(), nullable=False),
    sa.Column('zone_id', sa.String(), nullable=True),
    sa.Column('geometry_kind', sa.String(), nullable=False),
    sa.Column('geometry_json', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['camera_id'], ['camera.id'], ),
    sa.ForeignKeyConstraint(['zone_id'], ['zone.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_camera_coverage_camera_id'), 'camera_coverage', ['camera_id'], unique=False)
    op.create_index(op.f('ix_camera_coverage_zone_id'), 'camera_coverage', ['zone_id'], unique=False)
    op.add_column('camera', sa.Column('video_path', sa.String(), nullable=True))
    op.add_column('camera', sa.Column('reference_image_path', sa.String(), nullable=True))
    op.add_column('camera', sa.Column('start_time', sa.DateTime(), nullable=True))
    op.add_column('camera', sa.Column('sample_fps', sa.Float(), nullable=True))
    op.add_column('camera', sa.Column('confidence_threshold', sa.Float(), nullable=True))
    op.add_column('camera', sa.Column('queue_completion_seconds', sa.Integer(), nullable=True))
    op.add_column('camera', sa.Column('queue_abandonment_seconds', sa.Integer(), nullable=True))
    op.add_column('zone', sa.Column('map_polygon_json', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('zone', 'map_polygon_json')
    op.drop_column('camera', 'queue_abandonment_seconds')
    op.drop_column('camera', 'queue_completion_seconds')
    op.drop_column('camera', 'confidence_threshold')
    op.drop_column('camera', 'sample_fps')
    op.drop_column('camera', 'start_time')
    op.drop_column('camera', 'reference_image_path')
    op.drop_column('camera', 'video_path')
    op.drop_index(op.f('ix_camera_coverage_zone_id'), table_name='camera_coverage')
    op.drop_index(op.f('ix_camera_coverage_camera_id'), table_name='camera_coverage')
    op.drop_table('camera_coverage')
    op.drop_index(op.f('ix_map_store_id'), table_name='map')
    op.drop_table('map')
