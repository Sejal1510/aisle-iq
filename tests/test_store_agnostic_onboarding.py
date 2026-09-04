# P7 acceptance test: proves AisleIQ is genuinely store-agnostic, not just
# "ST1001/ST1002 still work after migration" (that equivalence is covered by
# tests/test_legacy_config_migration.py).
#
# A completely fictitious, non-Purplle, non-cosmetics retailer ("Northwind
# Electronics") is onboarded PURELY through StoreConfigService -- the same
# calls the onboarding API in app/api/onboarding.py makes -- with zero
# references to any Purplle-specific id, name, or polygon. The test then
# drives the *unchanged* pipeline/video/events.py + app/services event
# ingestion + analytics stack against that configuration and asserts real
# metrics come back, proving the downstream engine never needed to know
# this store's zone names or business category.
#
# TrackSnapshot objects are constructed directly (no real video/YOLO/
# ByteTrack involved) -- the same technique tests/test_video_event_generation.py
# already uses -- because what's under test here is the spatial-configuration
# -> event -> analytics path, not detection/tracking itself.
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.enums import ZoneType
from app.services.event_ingestion_service import EventIngestionService
from app.services.occupancy_service import OccupancyService
from app.services.queue_service import QueueService
from app.services.store_config_service import (
    LineGeometry,
    PolygonGeometry,
    StoreConfigService,
)
from app.services.time_series_service import TimeSeriesService
from pipeline.video.config import load_video_configs_from_db
from pipeline.video.events import VideoEventGenerator
from pipeline.video.tracking import TrackSnapshot

STORE_ID = "ST_ELECTRONICS_9001"
BASE_TIME = datetime(2027, 1, 4, 9, 0, 0)


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


def _snapshot(camera_id: str, track_id: str, timestamp: datetime, normalized_xy: tuple[float, float]) -> TrackSnapshot:
    width, height = 200, 200
    x, y = normalized_xy[0] * width, normalized_xy[1] * height
    return TrackSnapshot(
        track_id=track_id,
        store_id=STORE_ID,
        camera_id=camera_id,
        frame_index=0,
        timestamp=timestamp,
        bbox_xyxy=(x - 5, y - 20, x + 5, y),
        confidence=0.91,
        footpoint=(x, y),
        frame_size=(width, height),
    )


def _onboard_electronics_store(db) -> None:
    """Everything a retailer would do through the onboarding API -- no
    Purplle-specific concept (skincare/makeup/fragrance/billing polygon
    coordinates, ST1001/ST1002 ids, etc.) appears anywhere below."""
    from app.models.store import Store

    db.add(Store(id=STORE_ID, name="Northwind Electronics"))
    db.flush()

    config = StoreConfigService(db)

    config.create_zone(STORE_ID, "Z_MOBILES", name="Mobiles", zone_type=ZoneType.SHELF, is_revenue_zone=True)
    config.create_zone(STORE_ID, "Z_LAPTOPS", name="Laptops", zone_type=ZoneType.SHELF, is_revenue_zone=True)
    config.create_zone(STORE_ID, "Z_ACCESSORIES", name="Accessories", zone_type=ZoneType.DISPLAY, is_revenue_zone=True)
    config.create_zone(STORE_ID, "Z_BILLING", name="Billing", zone_type=ZoneType.CHECKOUT, is_revenue_zone=True)

    config.create_camera(
        STORE_ID, "CAM_ENTRY", role="entry", video_path="videos/northwind/entry.mp4", start_time=BASE_TIME
    )
    config.set_coverage(
        "CAM_ENTRY", zone_id=None, geometry=LineGeometry(axis="y", position=0.5, inside_greater_than_position=True)
    )

    config.create_camera(
        STORE_ID, "CAM_MOBILES", role="zone", video_path="videos/northwind/mobiles.mp4", start_time=BASE_TIME
    )
    config.set_coverage(
        "CAM_MOBILES",
        zone_id="Z_MOBILES",
        geometry=PolygonGeometry(points=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))),
    )

    config.create_camera(
        STORE_ID, "CAM_BILLING", role="billing", video_path="videos/northwind/billing.mp4", start_time=BASE_TIME
    )
    config.set_coverage(
        "CAM_BILLING",
        zone_id="Z_BILLING",
        geometry=PolygonGeometry(points=((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8))),
    )


def test_onboarded_electronics_store_produces_real_events(db_session):
    _onboard_electronics_store(db_session)
    db_session.commit()

    configs = {c.camera_id: c for c in load_video_configs_from_db(db_session, store_id=STORE_ID)}
    assert set(configs) == {"CAM_ENTRY", "CAM_MOBILES", "CAM_BILLING"}
    assert configs["CAM_MOBILES"].zones[0].name == "Mobiles"
    assert configs["CAM_MOBILES"].zones[0].type == "SHELF"
    assert configs["CAM_BILLING"].queue_zone_id == "Z_BILLING"

    entry_generator = VideoEventGenerator(configs["CAM_ENTRY"])
    mobiles_generator = VideoEventGenerator(configs["CAM_MOBILES"])
    billing_generator = VideoEventGenerator(configs["CAM_BILLING"])

    events: list[dict] = []
    t = BASE_TIME
    events += entry_generator.process_snapshot(_snapshot("CAM_ENTRY", "1", t, (0.5, 0.4)))
    events += entry_generator.process_snapshot(_snapshot("CAM_ENTRY", "1", t + timedelta(seconds=1), (0.5, 0.6)))
    events += mobiles_generator.process_snapshot(_snapshot("CAM_MOBILES", "1", t + timedelta(seconds=5), (0.5, 0.5)))
    events += billing_generator.process_snapshot(_snapshot("CAM_BILLING", "1", t + timedelta(seconds=60), (0.5, 0.5)))
    events += billing_generator.process_snapshot(
        _snapshot("CAM_BILLING", "1", t + timedelta(seconds=120), (1.5, 1.5))
    )
    events += entry_generator.process_snapshot(
        _snapshot("CAM_ENTRY", "1", t + timedelta(seconds=130), (0.5, 0.4))
    )

    event_types = [e["event_type"] for e in events]
    assert "ENTRY" in event_types
    assert "ZONE_ENTER" in event_types
    assert "BILLING_QUEUE_JOIN" in event_types
    assert "BILLING_QUEUE_COMPLETE" in event_types
    assert "EXIT" in event_types
    # None of the generic pipeline code above referenced "Mobiles"/"Billing"
    # -- it only ever saw zone_id/type/name that store configuration supplied.


def test_onboarded_electronics_store_flows_through_ingestion_and_analytics(db_session):
    """The real acceptance test: unmodified EventIngestionService/
    OccupancyService/TimeSeriesService (no changes made for this store, or
    for P7 at all) produce correct analytics for a store the engine has
    never seen a special case for."""
    _onboard_electronics_store(db_session)
    db_session.commit()

    configs = {c.camera_id: c for c in load_video_configs_from_db(db_session, store_id=STORE_ID)}
    entry_generator = VideoEventGenerator(configs["CAM_ENTRY"])
    mobiles_generator = VideoEventGenerator(configs["CAM_MOBILES"])
    billing_generator = VideoEventGenerator(configs["CAM_BILLING"])

    ingestion = EventIngestionService(db_session)
    t = BASE_TIME
    raw_events: list[dict] = []
    raw_events += entry_generator.process_snapshot(_snapshot("CAM_ENTRY", "42", t, (0.5, 0.4)))
    raw_events += entry_generator.process_snapshot(
        _snapshot("CAM_ENTRY", "42", t + timedelta(seconds=1), (0.5, 0.6))
    )
    raw_events += mobiles_generator.process_snapshot(
        _snapshot("CAM_MOBILES", "42", t + timedelta(seconds=10), (0.5, 0.5))
    )
    raw_events += billing_generator.process_snapshot(
        _snapshot("CAM_BILLING", "42", t + timedelta(seconds=60), (0.5, 0.5))
    )
    raw_events += billing_generator.process_snapshot(
        _snapshot("CAM_BILLING", "42", t + timedelta(seconds=110), (1.5, 1.5))
    )
    raw_events += entry_generator.process_snapshot(
        _snapshot("CAM_ENTRY", "42", t + timedelta(seconds=115), (0.5, 0.4))
    )

    from app.schemas.event import CanonicalEvent

    for raw in raw_events:
        payload = CanonicalEvent.model_validate(raw)
        ingestion.process_event(payload, run_inference=False)
    db_session.commit()

    # Sampled before the zone-camera event (t+10s): only the entry-scoped
    # identity has an open session at this instant. (Sampling after t+10s
    # would also show the zone-camera-scoped identity as occupying -- a
    # separate TrackedEntity, since this system does not perform
    # cross-camera identity merging/ReID; see README's documented
    # limitation. That is pre-existing, unrelated-to-P7 behavior, not
    # something this test is exercising.)
    occupancy = OccupancyService(db_session).current_occupancy(STORE_ID, as_of=t + timedelta(seconds=5))
    assert occupancy.occupancy == 1

    footfall = TimeSeriesService(db_session).hourly_footfall(
        STORE_ID, t - timedelta(minutes=1), t + timedelta(hours=1), bucket_minutes=60
    )
    assert sum(bucket.entries for bucket in footfall.buckets) == 1

    queue_metrics = QueueService(db_session).queue_metrics(
        STORE_ID, t - timedelta(minutes=1), t + timedelta(hours=1)
    )
    assert queue_metrics.completed_visits == 1
