# P8 milestones 1 & 2: the spatial configuration read model (Map/Zone/
# CameraCoverage assembly) and zone-intensity aggregation (Zone.map_polygon_json
# joined with AnalyticsService.zone_dwell_metrics, unchanged). Deliberately
# store-agnostic fixtures throughout -- no ST1001/ST1002/Purplle-specific
# assumptions -- mirroring tests/test_store_agnostic_onboarding.py's pattern.
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import EventType, ZoneType
from app.models.event import Event
from app.models.store import Store
from app.models.tracking import TrackedEntity, VisitSession
from app.services.spatial_intelligence_service import SpatialIntelligenceService
from app.services.store_config_service import PolygonGeometry, StoreConfigService

STORE_ID = "ST_SPATIAL_FICTITIOUS"


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


def _make_store(db_session: Session, store_id: str = STORE_ID) -> None:
    db_session.add(Store(id=store_id, name="Fictitious Test Retailer"))
    db_session.flush()


def _zone_visit(
    db_session: Session,
    *,
    store_id: str,
    zone_id: str,
    entry_time: datetime,
    dwell_seconds: int,
    entity_suffix: str,
) -> None:
    """A minimal ZONE_ENTERED/ZONE_EXITED pair -- the exact shape
    AnalyticsService.zone_dwell_metrics reads (see tests/test_analytics_service.py
    for the established pattern this mirrors)."""
    entity = TrackedEntity(id=f"entity-{entity_suffix}", store_id=store_id, is_staff=False)
    session = VisitSession(
        id=f"session-{entity_suffix}",
        tracked_entity_id=entity.id,
        store_id=store_id,
        entry_time=entry_time,
    )
    db_session.add_all([entity, session])
    db_session.flush()

    db_session.add_all(
        [
            Event(
                session_id=session.id,
                tracked_entity_id=entity.id,
                store_id=store_id,
                zone_id=zone_id,
                event_type=EventType.ZONE_ENTERED,
                timestamp=entry_time,
            ),
            Event(
                session_id=session.id,
                tracked_entity_id=entity.id,
                store_id=store_id,
                zone_id=zone_id,
                event_type=EventType.ZONE_EXITED,
                timestamp=entry_time + timedelta(seconds=dwell_seconds),
            ),
        ]
    )
    db_session.flush()


# ---------------------------------------------------------------------------
# Milestone 1: spatial configuration read model
# ---------------------------------------------------------------------------


def test_spatial_config_assembles_map_zones_and_covering_cameras(db_session: Session) -> None:
    _make_store(db_session)
    config = StoreConfigService(db_session)
    config.create_map(STORE_ID, name="Floor 1", file_path="data/maps/x/y.png", content_type="image/png",
                       width_px=800, height_px=600)
    zone = config.create_zone(
        STORE_ID, "ZONE_A", name="Zone A", zone_type=ZoneType.SHELF, is_revenue_zone=True,
        map_polygon=PolygonGeometry(points=((0.1, 0.1), (0.4, 0.1), (0.4, 0.4), (0.1, 0.4))),
    )
    camera = config.create_camera(STORE_ID, "CAM_A", role="zone")
    config.set_coverage(camera.id, zone_id=zone.id, geometry=PolygonGeometry(points=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))))
    db_session.commit()

    response = SpatialIntelligenceService(db_session).get_spatial_config(STORE_ID)

    assert response.store_id == STORE_ID
    assert response.layout_image_url is not None
    assert len(response.zones) == 1
    zone_out = response.zones[0]
    assert zone_out.zone_id == "ZONE_A"
    assert zone_out.zone_type == "SHELF"
    assert zone_out.is_revenue_zone is True
    assert zone_out.map_polygon == [(0.1, 0.1), (0.4, 0.1), (0.4, 0.4), (0.1, 0.4)]
    assert zone_out.covering_camera_ids == ["CAM_A"]


def test_spatial_config_zone_without_polygon_or_coverage_is_honestly_empty(db_session: Session) -> None:
    """A zone that exists (e.g. auto-created by ingestion, see
    ReferenceDataService.ensure_zone) but was never drawn on a map must report
    a null polygon and no covering cameras -- not a fabricated/guessed shape."""
    _make_store(db_session)
    StoreConfigService(db_session).create_zone(STORE_ID, "ZONE_UNCONFIGURED", name="Unconfigured", zone_type=ZoneType.OTHER)
    db_session.commit()

    response = SpatialIntelligenceService(db_session).get_spatial_config(STORE_ID)

    assert len(response.zones) == 1
    assert response.zones[0].map_polygon is None
    assert response.zones[0].covering_camera_ids == []


def test_spatial_config_store_with_no_map_returns_null_layout_image_url(db_session: Session) -> None:
    _make_store(db_session)
    db_session.commit()

    response = SpatialIntelligenceService(db_session).get_spatial_config(STORE_ID)

    assert response.layout_image_url is None
    assert response.zones == []


# ---------------------------------------------------------------------------
# Milestone 2: zone-intensity aggregation
# ---------------------------------------------------------------------------


def test_zone_intensity_reuses_real_zone_dwell_metric_values(db_session: Session) -> None:
    _make_store(db_session)
    base = datetime(2026, 7, 1, 9, 0, 0)
    _zone_visit(db_session, store_id=STORE_ID, zone_id="ZONE_A", entry_time=base, dwell_seconds=120, entity_suffix="1")
    _zone_visit(db_session, store_id=STORE_ID, zone_id="ZONE_A", entry_time=base + timedelta(minutes=10), dwell_seconds=180, entity_suffix="2")
    StoreConfigService(db_session).create_zone(STORE_ID, "ZONE_A", name="Zone A", zone_type=ZoneType.SHELF)
    db_session.commit()

    response = SpatialIntelligenceService(db_session).get_zone_intensity(STORE_ID)

    assert len(response.zones) == 1
    zone_a = response.zones[0]
    assert zone_a.visits == 2
    assert zone_a.total_dwell_seconds == 300
    assert zone_a.average_dwell_seconds == 150.0


def test_zone_intensity_ranks_by_visits_with_deterministic_tie_break(db_session: Session) -> None:
    _make_store(db_session)
    base = datetime(2026, 7, 1, 9, 0, 0)
    config = StoreConfigService(db_session)
    config.create_zone(STORE_ID, "ZONE_HIGH", name="High Traffic", zone_type=ZoneType.SHELF)
    config.create_zone(STORE_ID, "ZONE_LOW", name="Low Traffic", zone_type=ZoneType.SHELF)
    for i in range(3):
        _zone_visit(db_session, store_id=STORE_ID, zone_id="ZONE_HIGH", entry_time=base + timedelta(minutes=i), dwell_seconds=60, entity_suffix=f"high-{i}")
    _zone_visit(db_session, store_id=STORE_ID, zone_id="ZONE_LOW", entry_time=base, dwell_seconds=60, entity_suffix="low-0")
    db_session.commit()

    response = SpatialIntelligenceService(db_session).get_zone_intensity(STORE_ID, metric="visits")

    by_zone = {z.zone_id: z for z in response.zones}
    assert by_zone["ZONE_HIGH"].rank == 1
    assert by_zone["ZONE_HIGH"].intensity == 1.0
    assert by_zone["ZONE_LOW"].rank == 2
    assert by_zone["ZONE_LOW"].intensity < 1.0


def test_zone_intensity_metric_dwell_reorders_ranking_versus_visits(db_session: Session) -> None:
    """A zone with many brief visits should outrank a zone with one long visit
    under metric=visits but not under metric=dwell -- proving the metric
    selector actually changes which real values drive the ranking."""
    _make_store(db_session)
    base = datetime(2026, 7, 1, 9, 0, 0)
    config = StoreConfigService(db_session)
    config.create_zone(STORE_ID, "ZONE_BUSY", name="Busy, brief visits", zone_type=ZoneType.SHELF)
    config.create_zone(STORE_ID, "ZONE_STICKY", name="Rare, long visits", zone_type=ZoneType.SHELF)
    for i in range(5):
        _zone_visit(db_session, store_id=STORE_ID, zone_id="ZONE_BUSY", entry_time=base + timedelta(minutes=i), dwell_seconds=10, entity_suffix=f"busy-{i}")
    _zone_visit(db_session, store_id=STORE_ID, zone_id="ZONE_STICKY", entry_time=base, dwell_seconds=3600, entity_suffix="sticky-0")
    db_session.commit()

    by_visits = {z.zone_id: z for z in SpatialIntelligenceService(db_session).get_zone_intensity(STORE_ID, metric="visits").zones}
    by_dwell = {z.zone_id: z for z in SpatialIntelligenceService(db_session).get_zone_intensity(STORE_ID, metric="dwell").zones}

    assert by_visits["ZONE_BUSY"].rank == 1
    assert by_visits["ZONE_STICKY"].rank == 2
    assert by_dwell["ZONE_STICKY"].rank == 1
    assert by_dwell["ZONE_BUSY"].rank == 2
    # Raw values are identical between the two calls -- only rank/intensity change.
    assert by_visits["ZONE_BUSY"].visits == by_dwell["ZONE_BUSY"].visits == 5
    assert by_visits["ZONE_STICKY"].total_dwell_seconds == by_dwell["ZONE_STICKY"].total_dwell_seconds == 3600


def test_zone_intensity_includes_configured_zero_activity_zone(db_session: Session) -> None:
    """A zone that has a drawn map outline but zero recorded visits must still
    appear (at zero intensity), not silently vanish from the map."""
    _make_store(db_session)
    config = StoreConfigService(db_session)
    config.create_zone(
        STORE_ID, "ZONE_QUIET", name="Quiet Zone", zone_type=ZoneType.SHELF,
        map_polygon=PolygonGeometry(points=((0.0, 0.0), (0.2, 0.0), (0.2, 0.2))),
    )
    db_session.commit()

    response = SpatialIntelligenceService(db_session).get_zone_intensity(STORE_ID)

    assert len(response.zones) == 1
    assert response.zones[0].visits == 0
    assert response.zones[0].intensity == 0.0
    assert response.zones[0].rank == 1
    assert response.zones[0].map_polygon == [(0.0, 0.0), (0.2, 0.0), (0.2, 0.2)]


def test_zone_intensity_no_activity_anywhere_does_not_divide_by_zero(db_session: Session) -> None:
    _make_store(db_session)
    config = StoreConfigService(db_session)
    config.create_zone(STORE_ID, "ZONE_A", name="A", zone_type=ZoneType.SHELF)
    config.create_zone(STORE_ID, "ZONE_B", name="B", zone_type=ZoneType.SHELF)
    db_session.commit()

    response = SpatialIntelligenceService(db_session).get_zone_intensity(STORE_ID)

    assert all(z.intensity == 0.0 for z in response.zones)
    assert {z.rank for z in response.zones} == {1, 2}


def test_zone_intensity_rejects_unsupported_metric(db_session: Session) -> None:
    _make_store(db_session)
    db_session.commit()

    with pytest.raises(ValueError):
        SpatialIntelligenceService(db_session).get_zone_intensity(STORE_ID, metric="revenue")  # type: ignore[arg-type]


def test_zone_intensity_is_store_scoped(db_session: Session) -> None:
    _make_store(db_session, "ST_SPATIAL_A")
    _make_store(db_session, "ST_SPATIAL_B")
    config = StoreConfigService(db_session)
    config.create_zone("ST_SPATIAL_A", "ZONE_A", name="A", zone_type=ZoneType.SHELF)
    config.create_zone("ST_SPATIAL_B", "ZONE_B", name="B", zone_type=ZoneType.SHELF)
    db_session.commit()

    response = SpatialIntelligenceService(db_session).get_zone_intensity("ST_SPATIAL_A")

    assert [z.zone_id for z in response.zones] == ["ZONE_A"]
