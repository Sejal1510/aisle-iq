# P8 milestone 5: two clearly separated validation cases, per the P8 plan --
# never blur which dataset produced which result.
#
# (A) REAL CCTV-DERIVED DATA -- data/generated_cctv_events.jsonl, the mandatory
#     event-log deliverable produced by the actual YOLOv8+ByteTrack pipeline
#     against the challenge-provided videos (see README's "Dataset Provenance"
#     section). It only has two zone concepts per store ("MAIN_ZONE",
#     "BILLING_QUEUE") -- this test does NOT invent richer zone labels for it,
#     and does NOT fabricate a Map/polygon that was never actually configured
#     for these stores (see docs/DESIGN.md's P8 section for why: no Map image
#     has ever existed in this repository/environment for ST1001/ST1002). The
#     honest limitation -- real activity data with no spatial map on top of it
#     -- is itself part of what this test asserts.
#
# (B) SYNTHETIC DEMO DATA -- data/demo_cctv_events_st1001_st1002.jsonl, the
#     existing, already clearly-labeled-synthetic, deterministic dataset (see
#     README's "Synthetic CCTV Demo Data (P4)" section). This test configures
#     a Map + zone map_polygons for ITS zone ids (Makeup/Fragrance/Skincare)
#     to demonstrate the differentiated zone-shading experience end-to-end --
#     this configuration is test-only scaffolding, not a claim that the real
#     CCTV videos were ever spatially onboarded this way.
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import ZoneType
from app.schemas.event import EventPayload
from app.services.event_ingestion_service import EventIngestionService
from app.services.spatial_intelligence_service import SpatialIntelligenceService
from app.services.store_config_service import PolygonGeometry, StoreConfigService

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_EVENTS_PATH = PROJECT_ROOT / "data" / "generated_cctv_events.jsonl"
SYNTHETIC_EVENTS_PATH = PROJECT_ROOT / "data" / "demo_cctv_events_st1001_st1002.jsonl"

_event_adapter = TypeAdapter(EventPayload)


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _ingest_jsonl(db_session: Session, path: Path) -> None:
    service = EventIngestionService(db_session)
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            payload = _event_adapter.validate_python(json.loads(line))
            service.process_event(payload, run_inference=False)
    db_session.commit()


# ---------------------------------------------------------------------------
# (A) Real CCTV-derived data
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not REAL_EVENTS_PATH.is_file(), reason="data/generated_cctv_events.jsonl not present")
def test_real_cctv_data_produces_correct_zone_level_activity(db_session: Session) -> None:
    """The mandatory real event-log deliverable, ingested unmodified, must
    produce non-zero, real zone activity for ST1001_MAIN_ZONE -- the only
    genuine zone concept present in this data. No invented zone names."""
    _ingest_jsonl(db_session, REAL_EVENTS_PATH)

    response = SpatialIntelligenceService(db_session).get_zone_intensity("ST1001")

    by_zone = {z.zone_id: z for z in response.zones}
    assert "ST1001_MAIN_ZONE" in by_zone
    assert by_zone["ST1001_MAIN_ZONE"].visits > 0
    assert by_zone["ST1001_MAIN_ZONE"].total_dwell_seconds >= 0
    assert by_zone["ST1001_MAIN_ZONE"].rank == 1  # the only zone with any zone-dwell activity
    # ST1001_BILLING_QUEUE also exists as a Zone row (auto-created by ingesting
    # its real BILLING_QUEUE_ABANDON events -- see ReferenceDataService.ensure_zone),
    # but checkout dwell is a queue metric, not a zone-dwell one: it correctly
    # shows zero zone-level activity rather than being fabricated or hidden.
    assert set(by_zone.keys()) == {"ST1001_MAIN_ZONE", "ST1001_BILLING_QUEUE"}
    assert by_zone["ST1001_BILLING_QUEUE"].visits == 0


@pytest.mark.skipif(not REAL_EVENTS_PATH.is_file(), reason="data/generated_cctv_events.jsonl not present")
def test_real_cctv_data_has_no_spatial_map_configured(db_session: Session) -> None:
    """Documents the actual current limitation instead of hiding it: this
    store has real, non-zero zone activity but no uploaded Map and no drawn
    Zone.map_polygon anywhere in this repository/environment (see
    pipeline/migrate_legacy_store_config.py's own note that the legacy layout
    images have never been present here). The spatial-config read model must
    say so plainly -- null layout, null polygon -- not fabricate one."""
    _ingest_jsonl(db_session, REAL_EVENTS_PATH)

    config = SpatialIntelligenceService(db_session).get_spatial_config("ST1001")

    assert config.layout_image_url is None
    zone = next(z for z in config.zones if z.zone_id == "ST1001_MAIN_ZONE")
    assert zone.map_polygon is None
    assert zone.covering_camera_ids == []


# ---------------------------------------------------------------------------
# (B) Synthetic demo data
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not SYNTHETIC_EVENTS_PATH.is_file(), reason="data/demo_cctv_events_st1001_st1002.jsonl not present")
def test_synthetic_multi_zone_data_renders_differentiated_zone_intensity(db_session: Session) -> None:
    """Demonstrates the actual product experience -- a map with multiple
    configured zones shaded by differentiated real (synthetic-data-derived)
    intensity -- using the existing, already clearly-labeled synthetic
    dataset. The map/polygon configuration below is test scaffolding for this
    demonstration, not a claim about the real CCTV videos (see this file's
    module docstring)."""
    _ingest_jsonl(db_session, SYNTHETIC_EVENTS_PATH)

    config = StoreConfigService(db_session)
    config.create_map(
        "ST1001", name="Synthetic demo floor plan (test scaffolding)",
        file_path="data/maps/ST1001/demo.png", content_type="image/png", width_px=None, height_px=None,
    )
    # Synthetic dataset's own zone ids (see data/demo_cctv_events_st1001_st1002.jsonl) --
    # not invented here, only given a map outline they didn't previously have.
    for zone_id, name, polygon in [
        ("ST1001_MAKEUP", "Makeup", ((0.05, 0.05), (0.35, 0.05), (0.35, 0.35), (0.05, 0.35))),
        ("ST1001_FRAGRANCE", "Fragrance", ((0.4, 0.05), (0.7, 0.05), (0.7, 0.35), (0.4, 0.35))),
        ("ST1001_SKINCARE", "Skincare", ((0.05, 0.4), (0.35, 0.4), (0.35, 0.7), (0.05, 0.7))),
    ]:
        config.update_zone(zone_id, name=name, zone_type=ZoneType.SHELF, map_polygon=PolygonGeometry(points=polygon))
    db_session.commit()

    spatial_config = SpatialIntelligenceService(db_session).get_spatial_config("ST1001")
    intensity = SpatialIntelligenceService(db_session).get_zone_intensity("ST1001")

    assert spatial_config.layout_image_url is not None
    configured_zone_ids = {z.zone_id for z in spatial_config.zones if z.map_polygon is not None}
    assert {"ST1001_MAKEUP", "ST1001_FRAGRANCE", "ST1001_SKINCARE"}.issubset(configured_zone_ids)

    by_zone = {z.zone_id: z for z in intensity.zones}
    demo_zones = {zid: by_zone[zid] for zid in ("ST1001_MAKEUP", "ST1001_FRAGRANCE", "ST1001_SKINCARE")}
    # Real (synthetic-derived) visit counts, all present and not all identical --
    # proving the intensity values genuinely differentiate zones rather than
    # being a flat placeholder.
    assert all(z.visits > 0 for z in demo_zones.values())
    assert len({z.visits for z in demo_zones.values()}) > 1
    ranks = sorted(z.rank for z in demo_zones.values())
    assert ranks == list(range(ranks[0], ranks[0] + 3))  # three distinct, consecutive ranks
