from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.store import Camera
    from app.models.video_processing import VideoProcessingJob


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

    from app.models.store import Camera

    query = select(Camera).where(Camera.video_path.is_not(None), Camera.start_time.is_not(None))
    if store_id is not None:
        query = query.where(Camera.store_id == store_id)

    configs: list[VideoProcessingConfig] = []
    for camera in db.scalars(query.order_by(Camera.id)):
        config = _build_config_for_camera(db, camera, video_path=Path(camera.video_path))
        if config is not None:
            configs.append(config)

    return configs


def build_config_for_job(db: Session, job: VideoProcessingJob) -> VideoProcessingConfig:
    """Build the ``VideoProcessingConfig`` for one ``VideoProcessingJob``,
    using the job's own snapshotted ``video_path`` -- never the camera's
    current ``video_path`` -- so a later re-upload to the same camera (which
    overwrites ``Camera.video_path`` in place, see
    ``StoreConfigService.update_camera``) cannot change what an
    already-created job processes. Shares every other per-camera field
    (zones, entry_line, queue_zone_id, tuning fields) with
    ``load_video_configs_from_db`` via ``_build_config_for_camera`` -- the
    only thing that differs for job-scoped processing is where
    ``video_path`` comes from.

    Raises ``ValueError`` if the job's camera can't currently produce a
    valid config (not found, wrong store, missing ``start_time``,
    unrecognized role) -- unlike ``load_video_configs_from_db``'s silent
    skip (appropriate when loading whatever's ready across a whole store), a
    single job's own camera is expected to already be fully configured by
    the time a job exists for it, so a problem here is a real error, not
    routine partial onboarding.
    """
    from app.core.storage import resolve_video_path
    from app.models.store import Camera

    camera = db.get(Camera, job.camera_id)
    if camera is None or camera.store_id != job.store_id:
        raise ValueError(f"Camera {job.camera_id!r} not found for store {job.store_id!r}.")
    if camera.start_time is None:
        raise ValueError(f"Camera {job.camera_id!r} has no configured start_time.")

    # Defense-in-depth (see app.core.storage.resolve_video_path's docstring):
    # the primary check belongs at the write boundary that accepts
    # video_path values (the onboarding camera-config endpoints), but this
    # is the boundary closest to actually handing a path to the video
    # pipeline, so it is re-verified here rather than trusted.
    resolve_video_path(job.video_path)

    config = _build_config_for_camera(db, camera, video_path=Path(job.video_path))
    if config is None:
        raise ValueError(f"Camera {job.camera_id!r} has an unrecognized role {camera.role!r}.")
    return config


def _build_config_for_camera(db: Session, camera: Camera, *, video_path: Path) -> VideoProcessingConfig | None:
    """Shared by ``load_video_configs_from_db`` (``video_path`` = the
    camera's own, live) and ``build_config_for_job`` (``video_path`` = a
    job's snapshot) -- builds everything else (zones, entry_line,
    queue_zone_id, tuning fields) from the camera's current configuration
    either way; only the caller decides where ``video_path`` comes from.
    Returns ``None`` if the camera's role isn't recognized, mirroring
    ``load_video_configs_from_db``'s prior inline ``try/except: continue``.
    """
    from sqlalchemy import select

    from app.models.spatial import CameraCoverage
    from app.models.store import Zone
    from app.services.store_config_service import LineGeometry, parse_geometry_json

    try:
        role = CameraRole(camera.role)
    except ValueError:
        return None

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

    return VideoProcessingConfig(
        store_id=camera.store_id,
        camera_id=camera.id,
        role=role,
        video_path=video_path,
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

