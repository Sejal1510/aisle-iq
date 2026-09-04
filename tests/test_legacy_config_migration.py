# P7: verifies pipeline.migrate_legacy_store_config produces DB configuration
# that load_video_configs_from_db turns back into VideoProcessingConfig
# objects equivalent to the pre-P7 hardcoded default_video_configs() output.
# The expected values below are a frozen snapshot of that deleted function's
# literals (see git history / pipeline/video/config.py's docstring) -- not a
# call to it, since principle 9 requires it to be removed once migration is
# verified.
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from pipeline.migrate_legacy_store_config import migrate
from pipeline.video.config import (
    CameraRole,
    EntryLine,
    Point,
    PolygonZone,
    load_video_configs_from_db,
)

_STORE_1_START = datetime(2026, 6, 1, 10, 0, 0)
_STORE_2_START = datetime(2026, 6, 1, 10, 0, 0)

_STORE_1_ZONE = PolygonZone(
    id="ST1001_MAIN_ZONE",
    name="Store 1 Sales Floor",
    type="SHELF",
    is_revenue_zone=True,
    polygon=(Point(0.08, 0.10), Point(0.92, 0.10), Point(0.92, 0.88), Point(0.08, 0.88)),
)
_STORE_1_QUEUE = PolygonZone(
    id="ST1001_BILLING_QUEUE",
    name="Store 1 Billing Queue",
    type="CHECKOUT",  # was the unparseable literal "BILLING" pre-P7 -- see migration script docstring
    is_revenue_zone=True,
    polygon=(Point(0.55, 0.18), Point(0.95, 0.18), Point(0.95, 0.92), Point(0.55, 0.92)),
)
_STORE_2_ZONE = PolygonZone(
    id="ST1002_MAIN_ZONE",
    name="Store 2 Sales Floor",
    type="SHELF",
    is_revenue_zone=True,
    polygon=(Point(0.08, 0.14), Point(0.92, 0.14), Point(0.92, 0.90), Point(0.08, 0.90)),
)
_STORE_2_QUEUE = PolygonZone(
    id="ST1002_BILLING_QUEUE",
    name="Store 2 Billing Queue",
    type="CHECKOUT",
    is_revenue_zone=True,
    polygon=(Point(0.34, 0.08), Point(0.72, 0.08), Point(0.72, 0.45), Point(0.34, 0.45)),
)

_EXPECTED = {
    "ST1001_CAM_ZONE_1": {"store_id": "ST1001", "role": CameraRole.ZONE, "video_path": Path("data/Store 1/CAM 1 - zone.mp4"), "start_time": _STORE_1_START, "zones": (_STORE_1_ZONE,), "entry_line": None, "queue_zone_id": None},
    "ST1001_CAM_ZONE_2": {"store_id": "ST1001", "role": CameraRole.ZONE, "video_path": Path("data/Store 1/CAM 2 - zone.mp4"), "start_time": _STORE_1_START, "zones": (_STORE_1_ZONE,), "entry_line": None, "queue_zone_id": None},
    "ST1001_CAM_ENTRY": {"store_id": "ST1001", "role": CameraRole.ENTRY, "video_path": Path("data/Store 1/CAM 3 - entry.mp4"), "start_time": _STORE_1_START, "zones": (), "entry_line": EntryLine(axis="x", position=0.50, inside_greater_than_position=True), "queue_zone_id": None},
    "ST1001_CAM_BILLING": {"store_id": "ST1001", "role": CameraRole.BILLING, "video_path": Path("data/Store 1/CAM 5 - billing.mp4"), "start_time": _STORE_1_START, "zones": (_STORE_1_QUEUE,), "entry_line": None, "queue_zone_id": "ST1001_BILLING_QUEUE"},
    "ST1002_CAM_BILLING": {"store_id": "ST1002", "role": CameraRole.BILLING, "video_path": Path("data/Store 2/billing_area.mp4"), "start_time": _STORE_2_START, "zones": (_STORE_2_QUEUE,), "entry_line": None, "queue_zone_id": "ST1002_BILLING_QUEUE"},
    "ST1002_CAM_ENTRY_1": {"store_id": "ST1002", "role": CameraRole.ENTRY, "video_path": Path("data/Store 2/entry 1.mp4"), "start_time": _STORE_2_START, "zones": (), "entry_line": EntryLine(axis="y", position=0.55, inside_greater_than_position=False), "queue_zone_id": None},
    "ST1002_CAM_ENTRY_2": {"store_id": "ST1002", "role": CameraRole.ENTRY, "video_path": Path("data/Store 2/entry 2.mp4"), "start_time": _STORE_2_START, "zones": (), "entry_line": EntryLine(axis="y", position=0.55, inside_greater_than_position=False), "queue_zone_id": None},
    "ST1002_CAM_ZONE": {"store_id": "ST1002", "role": CameraRole.ZONE, "video_path": Path("data/Store 2/zone.mp4"), "start_time": _STORE_2_START, "zones": (_STORE_2_ZONE,), "entry_line": None, "queue_zone_id": None},
}


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_migrated_config_matches_legacy_snapshot(db_session):
    migrate(db_session)
    db_session.commit()

    configs = load_video_configs_from_db(db_session)
    by_camera = {c.camera_id: c for c in configs}

    assert set(by_camera) == set(_EXPECTED)
    for camera_id, expected in _EXPECTED.items():
        actual = by_camera[camera_id]
        assert actual.store_id == expected["store_id"]
        assert actual.role == expected["role"]
        assert actual.video_path == expected["video_path"]
        assert actual.start_time == expected["start_time"]
        assert actual.entry_line == expected["entry_line"]
        assert actual.queue_zone_id == expected["queue_zone_id"]
        assert len(actual.zones) == len(expected["zones"])
        for actual_zone, expected_zone in zip(actual.zones, expected["zones"], strict=True):
            assert actual_zone.id == expected_zone.id
            assert actual_zone.name == expected_zone.name
            assert actual_zone.type == expected_zone.type
            assert actual_zone.is_revenue_zone == expected_zone.is_revenue_zone
            assert actual_zone.polygon == expected_zone.polygon


def test_migration_is_idempotent(db_session):
    migrate(db_session)
    db_session.commit()
    first = {c.camera_id: c for c in load_video_configs_from_db(db_session)}

    migrate(db_session)
    db_session.commit()
    second = {c.camera_id: c for c in load_video_configs_from_db(db_session)}

    assert first.keys() == second.keys()
    for camera_id in first:
        assert first[camera_id].zones == second[camera_id].zones
        assert first[camera_id].entry_line == second[camera_id].entry_line


def test_migration_skips_missing_layout_images(db_session):
    summary = migrate(db_session)
    db_session.commit()
    # Neither legacy layout PNG has ever been committed to this repository
    # (see the P7 audit) -- the migration must not fabricate a Map row for a
    # file that does not exist on disk.
    assert summary["maps"] == []
    assert len(summary["skipped"]) == 2
