from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.spatial import GEOMETRY_KIND_POLYGON, CameraCoverage
from app.models.store import Zone
from app.schemas.spatial_intelligence import (
    SpatialZoneOut,
    StoreSpatialConfigResponse,
    StoreZoneIntensityResponse,
    ZoneIntensityMetric,
    ZoneIntensityOut,
)
from app.services.analytics_service import AnalyticsService
from app.services.heatmap_service import HeatmapService
from app.services.store_config_service import parse_geometry_json


class SpatialIntelligenceService:
    """P8: bridges the P7 spatial configuration layer (Map/Zone/CameraCoverage)
    with the existing, already-correct zone-level analytics engine.

    Deliberately does not attempt to place an individual event/visitor at a
    continuous position on the map -- see docs/DESIGN.md's "Spatial
    Configuration (P7)" section. The only spatial primitive trusted here is
    ``zone_id`` plus a zone's own ``map_polygon_json``, both of which already
    exist and are already correct; this service adds no new event-processing
    or coordinate logic, only assembly and aggregation of what's there.
    """

    def __init__(self, db: Session):
        self.db = db
        self._heatmap_service = HeatmapService(db)

    # ------------------------------------------------------------------
    # Milestone 1: spatial configuration read model
    # ------------------------------------------------------------------
    def get_spatial_config(self, store_id: str) -> StoreSpatialConfigResponse:
        zones = self.db.scalars(select(Zone).where(Zone.store_id == store_id).order_by(Zone.id)).all()
        covering_cameras = self._covering_cameras_by_zone(zone_ids=[zone.id for zone in zones])

        return StoreSpatialConfigResponse(
            store_id=store_id,
            layout_image_url=self._heatmap_service.layout_image_url(store_id),
            zones=[
                SpatialZoneOut(
                    zone_id=zone.id,
                    name=zone.name,
                    zone_type=zone.type.value,
                    is_revenue_zone=zone.is_revenue_zone,
                    map_polygon=_parse_map_polygon(zone.map_polygon_json),
                    covering_camera_ids=covering_cameras.get(zone.id, []),
                )
                for zone in zones
            ],
        )

    # ------------------------------------------------------------------
    # Milestone 2: zone-intensity aggregation
    # ------------------------------------------------------------------
    def get_zone_intensity(
        self, store_id: str, *, metric: ZoneIntensityMetric = "visits"
    ) -> StoreZoneIntensityResponse:
        if metric not in ("visits", "dwell"):
            raise ValueError(f"Unsupported metric: {metric!r}. Expected 'visits' or 'dwell'.")

        # Reuses AnalyticsService.zone_dwell_metrics verbatim -- no new
        # event-processing/aggregation logic. That method only returns zones
        # with at least one completed zone visit, so every *configured* zone
        # (from the Zone table) is left-joined against it below: a zone with
        # a drawn map outline but zero recorded activity still renders, at
        # zero intensity, rather than silently disappearing from the map.
        dwell_by_zone = {m.zone_id: m for m in AnalyticsService(self.db).zone_dwell_metrics(store_id)}

        zones = self.db.scalars(select(Zone).where(Zone.store_id == store_id).order_by(Zone.id)).all()
        rows = []
        for zone in zones:
            metrics = dwell_by_zone.get(zone.id)
            visits = metrics.visits if metrics else 0
            average_dwell_seconds = metrics.average_dwell_seconds if metrics else 0.0
            total_dwell_seconds = metrics.total_dwell_seconds if metrics else 0
            rows.append(
                {
                    "zone": zone,
                    "visits": visits,
                    "average_dwell_seconds": average_dwell_seconds,
                    "total_dwell_seconds": total_dwell_seconds,
                    "metric_value": visits if metric == "visits" else total_dwell_seconds,
                }
            )

        # Deterministic ordering: highest metric_value first, zone_id as a
        # stable tie-break so equal-activity zones don't reorder between
        # calls/dialects. Rank is this order's 1-indexed position (ties get
        # distinct consecutive ranks, not a shared rank -- simplest and
        # unambiguous for a UI list; "N-way tie" is not a distinction this
        # zone-level view needs to make).
        rows.sort(key=lambda row: (-row["metric_value"], row["zone"].id))
        max_value = max((row["metric_value"] for row in rows), default=0)

        zone_outs = [
            ZoneIntensityOut(
                zone_id=row["zone"].id,
                name=row["zone"].name,
                zone_type=row["zone"].type.value,
                is_revenue_zone=row["zone"].is_revenue_zone,
                map_polygon=_parse_map_polygon(row["zone"].map_polygon_json),
                visits=row["visits"],
                average_dwell_seconds=row["average_dwell_seconds"],
                total_dwell_seconds=row["total_dwell_seconds"],
                intensity=round(row["metric_value"] / max_value, 4) if max_value else 0.0,
                rank=rank,
            )
            for rank, row in enumerate(rows, start=1)
        ]

        return StoreZoneIntensityResponse(
            store_id=store_id,
            layout_image_url=self._heatmap_service.layout_image_url(store_id),
            metric=metric,
            zones=zone_outs,
        )

    def _covering_cameras_by_zone(self, *, zone_ids: list[str]) -> dict[str, list[str]]:
        if not zone_ids:
            return {}
        rows = self.db.execute(
            select(CameraCoverage.zone_id, CameraCoverage.camera_id)
            .where(CameraCoverage.zone_id.in_(zone_ids))
            .order_by(CameraCoverage.camera_id)
        ).all()
        result: dict[str, list[str]] = {}
        for zone_id, camera_id in rows:
            result.setdefault(zone_id, []).append(camera_id)
        return result


def _parse_map_polygon(map_polygon_json: str | None) -> list[tuple[float, float]] | None:
    if map_polygon_json is None:
        return None
    geometry = parse_geometry_json(GEOMETRY_KIND_POLYGON, map_polygon_json)
    return list(geometry.points)
