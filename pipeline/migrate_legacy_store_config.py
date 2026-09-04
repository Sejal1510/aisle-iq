"""P7 one-off migration: write ST1001/ST1002's config into the database.

Before P7, ``pipeline/video/config.py``'s ``default_video_configs()`` and
``app/services/heatmap_service.py``'s ``STORE_LAYOUTS`` dict hardcoded these
two stores' cameras, zones, polygon geometry, and layout image paths as
Python literals -- exactly the "store-specific configuration in source code"
problem the P7 audit identified. This script writes the same information as
persisted ``Camera``/``Zone``/``CameraCoverage``/``Map`` rows so
``pipeline.video.config.load_video_configs_from_db`` produces equivalent
``VideoProcessingConfig`` objects without either hardcoded source existing
anymore.

Not a general-purpose onboarding tool -- it is intentionally a one-time,
throwaway script for these two already-known stores, kept for historical
reproducibility (see docs/CHOICES.md). New stores are onboarded through the
``/stores`` + ``/stores/{store_id}/config/...`` API (or the ``onboarding/``
UI on top of it), never through a script like this one.

Coordinates below are copied verbatim from the pre-P7
``default_video_configs()`` -- see tests/test_legacy_config_migration.py for
the equivalence check against that frozen snapshot. The one deliberate,
non-geometric change: the queue zones' type string moves from the
un-parseable literal ``"BILLING"`` (which ``ZoneType`` has never actually had
a member for -- it silently fell back to ``OTHER`` whenever a zone was
auto-created from ingested CV events, see
``ReferenceDataService._parse_zone_type``) to ``ZoneType.CHECKOUT``, which
``ZoneType`` already models. This does not change any polygon coordinate.

Run with: python -m pipeline.migrate_legacy_store_config
"""
from __future__ import annotations

from datetime import datetime

import structlog

from app.db.session import SessionLocal, init_db
from app.models.enums import ZoneType
from app.models.store import Camera
from app.services.reference_data_service import ReferenceDataService
from app.services.store_config_service import (
    LineGeometry,
    PolygonGeometry,
    StoreConfigError,
    StoreConfigService,
)

logger = structlog.get_logger(__name__)

_STORE_1_START = datetime(2026, 6, 1, 10, 0, 0)
_STORE_2_START = datetime(2026, 6, 1, 10, 0, 0)

_STORE_1_ZONE_POLYGON = ((0.08, 0.10), (0.92, 0.10), (0.92, 0.88), (0.08, 0.88))
_STORE_1_QUEUE_POLYGON = ((0.55, 0.18), (0.95, 0.18), (0.95, 0.92), (0.55, 0.92))
_STORE_2_ZONE_POLYGON = ((0.08, 0.14), (0.92, 0.14), (0.92, 0.90), (0.08, 0.90))
_STORE_2_QUEUE_POLYGON = ((0.34, 0.08), (0.72, 0.08), (0.72, 0.45), (0.34, 0.45))

# Legacy layout image paths (STORE_LAYOUTS, pre-P7). Neither file has ever
# been present in this repository/checkout (raw challenge assets are
# git-ignored -- see the P7 audit's Actual Input Audit section); a Map row is
# only created if the file genuinely exists on the machine running this
# script.
_LEGACY_LAYOUT_PATHS = {
    "ST1001": "data/Store 1/Store 1 - layout.png",
    "ST1002": "data/Store 2/store 2 - layout.png",
}


def migrate(db) -> dict:
    reference_data = ReferenceDataService(db)
    config = StoreConfigService(db)
    summary: dict[str, list[str]] = {"stores": [], "zones": [], "cameras": [], "coverage": [], "maps": [], "skipped": []}

    reference_data.ensure_store("ST1001")
    reference_data.ensure_store("ST1002")
    summary["stores"] = ["ST1001", "ST1002"]

    zone_specs = [
        ("ST1001", "ST1001_MAIN_ZONE", "Store 1 Sales Floor", ZoneType.SHELF, True, _STORE_1_ZONE_POLYGON),
        ("ST1001", "ST1001_BILLING_QUEUE", "Store 1 Billing Queue", ZoneType.CHECKOUT, True, _STORE_1_QUEUE_POLYGON),
        ("ST1002", "ST1002_MAIN_ZONE", "Store 2 Sales Floor", ZoneType.SHELF, True, _STORE_2_ZONE_POLYGON),
        ("ST1002", "ST1002_BILLING_QUEUE", "Store 2 Billing Queue", ZoneType.CHECKOUT, True, _STORE_2_QUEUE_POLYGON),
    ]
    for store_id, zone_id, name, zone_type, is_revenue, polygon in zone_specs:
        _get_or_create_zone(config, store_id, zone_id, name, zone_type, is_revenue, polygon)
        summary["zones"].append(zone_id)

    camera_specs = [
        ("ST1001", "ST1001_CAM_ZONE_1", "zone", "data/Store 1/CAM 1 - zone.mp4", _STORE_1_START, [("ST1001_MAIN_ZONE", _STORE_1_ZONE_POLYGON)], None),
        ("ST1001", "ST1001_CAM_ZONE_2", "zone", "data/Store 1/CAM 2 - zone.mp4", _STORE_1_START, [("ST1001_MAIN_ZONE", _STORE_1_ZONE_POLYGON)], None),
        ("ST1001", "ST1001_CAM_ENTRY", "entry", "data/Store 1/CAM 3 - entry.mp4", _STORE_1_START, [], LineGeometry(axis="x", position=0.50, inside_greater_than_position=True)),
        ("ST1001", "ST1001_CAM_BILLING", "billing", "data/Store 1/CAM 5 - billing.mp4", _STORE_1_START, [("ST1001_BILLING_QUEUE", _STORE_1_QUEUE_POLYGON)], None),
        ("ST1002", "ST1002_CAM_BILLING", "billing", "data/Store 2/billing_area.mp4", _STORE_2_START, [("ST1002_BILLING_QUEUE", _STORE_2_QUEUE_POLYGON)], None),
        ("ST1002", "ST1002_CAM_ENTRY_1", "entry", "data/Store 2/entry 1.mp4", _STORE_2_START, [], LineGeometry(axis="y", position=0.55, inside_greater_than_position=False)),
        ("ST1002", "ST1002_CAM_ENTRY_2", "entry", "data/Store 2/entry 2.mp4", _STORE_2_START, [], LineGeometry(axis="y", position=0.55, inside_greater_than_position=False)),
        ("ST1002", "ST1002_CAM_ZONE", "zone", "data/Store 2/zone.mp4", _STORE_2_START, [("ST1002_MAIN_ZONE", _STORE_2_ZONE_POLYGON)], None),
    ]
    for store_id, camera_id, role, video_path, start_time, zone_coverage, entry_line in camera_specs:
        camera = db.get(Camera, camera_id)
        if camera is None:
            camera = config.create_camera(
                store_id, camera_id, name=None, role=role, video_path=video_path, start_time=start_time
            )
        summary["cameras"].append(camera_id)

        if not config.list_coverage(camera_id):
            if entry_line is not None:
                config.set_coverage(camera_id, zone_id=None, geometry=entry_line)
                summary["coverage"].append(f"{camera_id}:entry_line")
            for zone_id, polygon in zone_coverage:
                config.set_coverage(camera_id, zone_id=zone_id, geometry=PolygonGeometry(points=polygon))
                summary["coverage"].append(f"{camera_id}:{zone_id}")

    for store_id, legacy_path in _LEGACY_LAYOUT_PATHS.items():

        from app.core.storage import PROJECT_ROOT

        absolute_path = PROJECT_ROOT / legacy_path
        if not absolute_path.is_file():
            summary["skipped"].append(f"{store_id}: layout image not present at {legacy_path}")
            continue
        try:
            config.create_map(
                store_id,
                name=f"{store_id} layout (migrated)",
                file_path=legacy_path,
                content_type="image/png",
                width_px=None,
                height_px=None,
            )
            summary["maps"].append(store_id)
        except StoreConfigError as exc:
            summary["skipped"].append(f"{store_id}: {exc}")

    return summary


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        summary = migrate(db)
        db.commit()
        logger.info("legacy_store_config_migrated", **summary)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _get_or_create_zone(config, store_id, zone_id, name, zone_type, is_revenue, polygon):
    from app.models.store import Zone

    if config.db.get(Zone, zone_id) is not None:
        return
    config.create_zone(
        store_id,
        zone_id,
        name=name,
        zone_type=zone_type,
        is_revenue_zone=is_revenue,
        map_polygon=PolygonGeometry(points=polygon),
    )


if __name__ == "__main__":
    main()
