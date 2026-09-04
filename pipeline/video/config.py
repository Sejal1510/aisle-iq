from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class CameraRole(str, Enum):
    ENTRY = "entry"
    ZONE = "zone"
    BILLING = "billing"


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class PolygonZone:
    id: str
    name: str
    type: str
    is_revenue_zone: bool
    polygon: tuple[Point, ...]


@dataclass(frozen=True)
class EntryLine:
    axis: str
    position: float
    inside_greater_than_position: bool = True


@dataclass(frozen=True)
class VideoProcessingConfig:
    store_id: str
    camera_id: str
    role: CameraRole
    video_path: Path
    start_time: datetime
    zones: tuple[PolygonZone, ...] = field(default_factory=tuple)
    entry_line: EntryLine | None = None
    queue_zone_id: str | None = None
    sample_fps: float = 2.0
    confidence_threshold: float = 0.35
    queue_completion_seconds: int = 45
    queue_abandonment_seconds: int = 8


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


def load_video_configs_from_db(db: Session, store_id: str | None = None) -> list[VideoProcessingConfig]:
    """Build ``VideoProcessingConfig`` objects from persisted store
    configuration (P7) instead of Python literals -- this is the
    store-agnostic replacement for the old, now-deleted
    ``default_video_configs()`` (see ``pipeline.migrate_legacy_store_config``
    and ``tests/test_legacy_config_migration.py``).

    ``VideoProcessingConfig``/``PolygonZone``/``EntryLine`` themselves are
    unchanged; only how they get constructed changes, so
    ``pipeline.video.tracking``/``pipeline.video.events`` need no changes at
    all. A ``Camera`` row only produces a config if it has a recognized
    ``role``, a ``video_path``, and a ``start_time`` -- a camera that is
    registered but not yet fully configured for offline video processing is
    silently skipped rather than failing the whole store, since onboarding
    (map/zones/cameras) can legitimately be a work in progress.
    """
    from sqlalchemy import select

    from app.models.spatial import CameraCoverage
    from app.models.store import Camera, Zone
    from app.services.store_config_service import LineGeometry, parse_geometry_json

    query = select(Camera).where(Camera.video_path.is_not(None), Camera.start_time.is_not(None))
    if store_id is not None:
        query = query.where(Camera.store_id == store_id)

    configs: list[VideoProcessingConfig] = []
    for camera in db.scalars(query.order_by(Camera.id)):
        try:
            role = CameraRole(camera.role)
        except ValueError:
            continue

        zones: list[PolygonZone] = []
        entry_line: EntryLine | None = None
        queue_zone_id: str | None = None

        coverage_rows = db.scalars(
            select(CameraCoverage).where(CameraCoverage.camera_id == camera.id).order_by(CameraCoverage.created_at)
        )
        for coverage in coverage_rows:
            geometry = parse_geometry_json(coverage.geometry_kind, coverage.geometry_json)

            if isinstance(geometry, LineGeometry):
                entry_line = EntryLine(
                    axis=geometry.axis,
                    position=geometry.position,
                    inside_greater_than_position=geometry.inside_greater_than_position,
                )
                continue

            if coverage.zone_id is None:
                continue
            zone = db.get(Zone, coverage.zone_id)
            if zone is None:
                continue
            zones.append(
                PolygonZone(
                    id=zone.id,
                    name=zone.name,
                    type=zone.type.value,
                    is_revenue_zone=zone.is_revenue_zone,
                    polygon=tuple(Point(x, y) for x, y in geometry.points),
                )
            )
            if role == CameraRole.BILLING and queue_zone_id is None:
                queue_zone_id = zone.id

        configs.append(
            VideoProcessingConfig(
                store_id=camera.store_id,
                camera_id=camera.id,
                role=role,
                video_path=Path(camera.video_path),
                start_time=camera.start_time,
                zones=tuple(zones),
                entry_line=entry_line,
                queue_zone_id=queue_zone_id,
                sample_fps=camera.sample_fps if camera.sample_fps is not None else 2.0,
                confidence_threshold=(
                    camera.confidence_threshold if camera.confidence_threshold is not None else 0.35
                ),
                queue_completion_seconds=(
                    camera.queue_completion_seconds if camera.queue_completion_seconds is not None else 45
                ),
                queue_abandonment_seconds=(
                    camera.queue_abandonment_seconds if camera.queue_abandonment_seconds is not None else 8
                ),
            )
        )

    return configs

