from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


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


def default_video_configs() -> list[VideoProcessingConfig]:
    """Return conservative defaults for the provided Store 1 and Store 2 assets.

    Coordinates are normalized to frame width/height. They are intentionally
    configuration data, not business logic, so they can be calibrated later from
    the layout images without changing the pipeline.
    """
    store_1_start = datetime(2026, 6, 1, 10, 0, 0)
    store_2_start = datetime(2026, 6, 1, 10, 0, 0)

    store_1_zone = PolygonZone(
        id="ST1001_MAIN_ZONE",
        name="Store 1 Sales Floor",
        type="SHELF",
        is_revenue_zone=True,
        polygon=(
            Point(0.08, 0.10),
            Point(0.92, 0.10),
            Point(0.92, 0.88),
            Point(0.08, 0.88),
        ),
    )
    store_1_queue = PolygonZone(
        id="ST1001_BILLING_QUEUE",
        name="Store 1 Billing Queue",
        type="BILLING",
        is_revenue_zone=True,
        polygon=(
            Point(0.55, 0.18),
            Point(0.95, 0.18),
            Point(0.95, 0.92),
            Point(0.55, 0.92),
        ),
    )
    store_2_zone = PolygonZone(
        id="ST1002_MAIN_ZONE",
        name="Store 2 Sales Floor",
        type="SHELF",
        is_revenue_zone=True,
        polygon=(
            Point(0.08, 0.14),
            Point(0.92, 0.14),
            Point(0.92, 0.90),
            Point(0.08, 0.90),
        ),
    )
    store_2_queue = PolygonZone(
        id="ST1002_BILLING_QUEUE",
        name="Store 2 Billing Queue",
        type="BILLING",
        is_revenue_zone=True,
        polygon=(
            Point(0.34, 0.08),
            Point(0.72, 0.08),
            Point(0.72, 0.45),
            Point(0.34, 0.45),
        ),
    )

    return [
        VideoProcessingConfig(
            store_id="ST1001",
            camera_id="ST1001_CAM_ZONE_1",
            role=CameraRole.ZONE,
            video_path=DATA_DIR / "Store 1" / "CAM 1 - zone.mp4",
            start_time=store_1_start,
            zones=(store_1_zone,),
        ),
        VideoProcessingConfig(
            store_id="ST1001",
            camera_id="ST1001_CAM_ZONE_2",
            role=CameraRole.ZONE,
            video_path=DATA_DIR / "Store 1" / "CAM 2 - zone.mp4",
            start_time=store_1_start,
            zones=(store_1_zone,),
        ),
        VideoProcessingConfig(
            store_id="ST1001",
            camera_id="ST1001_CAM_ENTRY",
            role=CameraRole.ENTRY,
            video_path=DATA_DIR / "Store 1" / "CAM 3 - entry.mp4",
            start_time=store_1_start,
            entry_line=EntryLine(axis="x", position=0.50, inside_greater_than_position=True),
        ),
        VideoProcessingConfig(
            store_id="ST1001",
            camera_id="ST1001_CAM_BILLING",
            role=CameraRole.BILLING,
            video_path=DATA_DIR / "Store 1" / "CAM 5 - billing.mp4",
            start_time=store_1_start,
            zones=(store_1_queue,),
            queue_zone_id=store_1_queue.id,
        ),
        VideoProcessingConfig(
            store_id="ST1002",
            camera_id="ST1002_CAM_BILLING",
            role=CameraRole.BILLING,
            video_path=DATA_DIR / "Store 2" / "billing_area.mp4",
            start_time=store_2_start,
            zones=(store_2_queue,),
            queue_zone_id=store_2_queue.id,
        ),
        VideoProcessingConfig(
            store_id="ST1002",
            camera_id="ST1002_CAM_ENTRY_1",
            role=CameraRole.ENTRY,
            video_path=DATA_DIR / "Store 2" / "entry 1.mp4",
            start_time=store_2_start,
            entry_line=EntryLine(axis="y", position=0.55, inside_greater_than_position=False),
        ),
        VideoProcessingConfig(
            store_id="ST1002",
            camera_id="ST1002_CAM_ENTRY_2",
            role=CameraRole.ENTRY,
            video_path=DATA_DIR / "Store 2" / "entry 2.mp4",
            start_time=store_2_start,
            entry_line=EntryLine(axis="y", position=0.55, inside_greater_than_position=False),
        ),
        VideoProcessingConfig(
            store_id="ST1002",
            camera_id="ST1002_CAM_ZONE",
            role=CameraRole.ZONE,
            video_path=DATA_DIR / "Store 2" / "zone.mp4",
            start_time=store_2_start,
            zones=(store_2_zone,),
        ),
    ]

