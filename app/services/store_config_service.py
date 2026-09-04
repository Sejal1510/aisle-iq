from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import ZoneType
from app.models.spatial import (
    GEOMETRY_KIND_LINE,
    GEOMETRY_KIND_POLYGON,
    CameraCoverage,
    Map,
)
from app.models.store import Camera, Store, Zone


class StoreConfigError(ValueError):
    """A caller-facing configuration error (bad geometry, unknown zone, etc.).
    Raised as ValueError's subclass so existing 400-on-ValueError routing
    conventions (see app.api.stores._run_ranged) apply without new wiring."""


@dataclass(frozen=True)
class PolygonGeometry:
    points: tuple[tuple[float, float], ...]

    def to_json(self) -> str:
        return json.dumps({"kind": GEOMETRY_KIND_POLYGON, "points": [list(p) for p in self.points]})


@dataclass(frozen=True)
class LineGeometry:
    axis: str
    position: float
    inside_greater_than_position: bool = True

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": GEOMETRY_KIND_LINE,
                "axis": self.axis,
                "position": self.position,
                "inside_greater_than_position": self.inside_greater_than_position,
            }
        )


def parse_geometry_json(kind: str, raw: str) -> PolygonGeometry | LineGeometry:
    data = json.loads(raw)
    if kind == GEOMETRY_KIND_POLYGON:
        return PolygonGeometry(points=tuple((float(p[0]), float(p[1])) for p in data["points"]))
    if kind == GEOMETRY_KIND_LINE:
        return LineGeometry(
            axis=data["axis"],
            position=float(data["position"]),
            inside_greater_than_position=bool(data.get("inside_greater_than_position", True)),
        )
    raise StoreConfigError(f"Unknown geometry kind: {kind!r}")


class StoreConfigService:
    """Human-authored (admin/onboarding) spatial and camera configuration for
    a store -- Map, Zone geometry, Camera registration/processing settings,
    and CameraCoverage (P7).

    Deliberately separate from ``ReferenceDataService``: that service is the
    ingestion-time get-or-create path (a Zone/Camera row appearing the first
    time an event references its id, with placeholder fields). This service
    is the operator-facing path for actually defining what a store's cameras
    and zones are before any video has ever been processed -- it never
    silently creates a Store; the store must already exist.
    """

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Map
    # ------------------------------------------------------------------
    def create_map(
        self,
        store_id: str,
        *,
        name: str | None,
        file_path: str,
        content_type: str | None,
        width_px: int | None,
        height_px: int | None,
    ) -> Map:
        self._require_store(store_id)
        # Only one active map per store keeps "the current layout" unambiguous
        # for the heatmap backdrop and the onboarding UI's default canvas --
        # a re-upload supersedes the previous one rather than accumulating
        # silently-ignored history.
        for existing in self.db.scalars(
            select(Map).where(Map.store_id == store_id, Map.is_active.is_(True))
        ):
            existing.is_active = False

        map_row = Map(
            id=str(uuid.uuid4()),
            store_id=store_id,
            name=name,
            file_path=file_path,
            content_type=content_type,
            width_px=width_px,
            height_px=height_px,
            is_active=True,
        )
        self.db.add(map_row)
        self.db.flush()
        return map_row

    def get_active_map(self, store_id: str) -> Map | None:
        return self.db.execute(
            select(Map)
            .where(Map.store_id == store_id, Map.is_active.is_(True))
            .order_by(Map.created_at.desc())
        ).scalars().first()

    def list_maps(self, store_id: str) -> list[Map]:
        return list(self.db.scalars(select(Map).where(Map.store_id == store_id).order_by(Map.created_at.desc())))

    # ------------------------------------------------------------------
    # Zone
    # ------------------------------------------------------------------
    def create_zone(
        self,
        store_id: str,
        zone_id: str,
        *,
        name: str,
        zone_type: ZoneType,
        is_revenue_zone: bool = False,
        map_polygon: PolygonGeometry | None = None,
    ) -> Zone:
        self._require_store(store_id)
        if self.db.get(Zone, zone_id) is not None:
            raise StoreConfigError(f"Zone '{zone_id}' already exists.")

        zone = Zone(
            id=zone_id,
            store_id=store_id,
            name=name,
            type=zone_type,
            is_revenue_zone=is_revenue_zone,
            map_polygon_json=map_polygon.to_json() if map_polygon else None,
        )
        self.db.add(zone)
        self.db.flush()
        return zone

    def update_zone(
        self,
        zone_id: str,
        *,
        name: str | None = None,
        zone_type: ZoneType | None = None,
        is_revenue_zone: bool | None = None,
        map_polygon: PolygonGeometry | None = None,
    ) -> Zone:
        zone = self._require_zone(zone_id)
        if name is not None:
            zone.name = name
        if zone_type is not None:
            zone.type = zone_type
        if is_revenue_zone is not None:
            zone.is_revenue_zone = is_revenue_zone
        if map_polygon is not None:
            zone.map_polygon_json = map_polygon.to_json()
        self.db.flush()
        return zone

    def list_zones(self, store_id: str) -> list[Zone]:
        return list(self.db.scalars(select(Zone).where(Zone.store_id == store_id).order_by(Zone.id)))

    # ------------------------------------------------------------------
    # Camera
    # ------------------------------------------------------------------
    def create_camera(
        self,
        store_id: str,
        camera_id: str,
        *,
        role: str,
        name: str | None = None,
        video_path: str | None = None,
        reference_image_path: str | None = None,
        start_time: datetime | None = None,
        sample_fps: float | None = None,
        confidence_threshold: float | None = None,
        queue_completion_seconds: int | None = None,
        queue_abandonment_seconds: int | None = None,
    ) -> Camera:
        self._require_store(store_id)
        if self.db.get(Camera, camera_id) is not None:
            raise StoreConfigError(f"Camera '{camera_id}' already exists.")

        camera = Camera(
            id=camera_id,
            store_id=store_id,
            name=name,
            role=role,
            video_path=video_path,
            reference_image_path=reference_image_path,
            start_time=start_time,
            sample_fps=sample_fps,
            confidence_threshold=confidence_threshold,
            queue_completion_seconds=queue_completion_seconds,
            queue_abandonment_seconds=queue_abandonment_seconds,
        )
        self.db.add(camera)
        self.db.flush()
        return camera

    def update_camera(self, camera_id: str, **fields) -> Camera:
        camera = self._require_camera(camera_id)
        for field, value in fields.items():
            if value is not None:
                setattr(camera, field, value)
        self.db.flush()
        return camera

    def list_cameras(self, store_id: str) -> list[Camera]:
        return list(self.db.scalars(select(Camera).where(Camera.store_id == store_id).order_by(Camera.id)))

    # ------------------------------------------------------------------
    # CameraCoverage
    # ------------------------------------------------------------------
    def set_coverage(
        self,
        camera_id: str,
        *,
        zone_id: str | None,
        geometry: PolygonGeometry | LineGeometry,
    ) -> CameraCoverage:
        self._require_camera(camera_id)
        if zone_id is not None:
            self._require_zone(zone_id)

        kind = GEOMETRY_KIND_POLYGON if isinstance(geometry, PolygonGeometry) else GEOMETRY_KIND_LINE
        coverage = CameraCoverage(
            id=str(uuid.uuid4()),
            camera_id=camera_id,
            zone_id=zone_id,
            geometry_kind=kind,
            geometry_json=geometry.to_json(),
        )
        self.db.add(coverage)
        self.db.flush()
        return coverage

    def list_coverage(self, camera_id: str) -> list[CameraCoverage]:
        return list(
            self.db.scalars(
                select(CameraCoverage).where(CameraCoverage.camera_id == camera_id).order_by(CameraCoverage.created_at)
            )
        )

    def delete_coverage(self, coverage_id: str) -> None:
        coverage = self.db.get(CameraCoverage, coverage_id)
        if coverage is None:
            raise StoreConfigError(f"Camera coverage '{coverage_id}' not found.")
        self.db.delete(coverage)
        self.db.flush()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _require_store(self, store_id: str) -> Store:
        store = self.db.get(Store, store_id)
        if store is None:
            raise StoreConfigError(f"Store '{store_id}' does not exist.")
        return store

    def _require_zone(self, zone_id: str) -> Zone:
        zone = self.db.get(Zone, zone_id)
        if zone is None:
            raise StoreConfigError(f"Zone '{zone_id}' does not exist.")
        return zone

    def _require_camera(self, camera_id: str) -> Camera:
        camera = self.db.get(Camera, camera_id)
        if camera is None:
            raise StoreConfigError(f"Camera '{camera_id}' does not exist.")
        return camera
