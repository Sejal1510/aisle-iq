from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.storage import resolve_relative_path
from app.models.event import Event
from app.models.spatial import Map
from app.models.tracking import VisitSession
from app.schemas.analytics import HeatmapPoint, StoreHeatmapResponse


@dataclass
class HeatmapBucket:
    count: int = 0
    sum_x: float = 0.0
    sum_y: float = 0.0

    def add(self, x: float, y: float) -> None:
        self.count += 1
        self.sum_x += x
        self.sum_y += y

    @property
    def center_x(self) -> float:
        return self.sum_x / self.count if self.count else 0.0

    @property
    def center_y(self) -> float:
        return self.sum_y / self.count if self.count else 0.0


class HeatmapService:
    def __init__(self, db: Session):
        self.db = db

    def get_store_heatmap(self, store_id: str, *, grid_size: int = 24) -> StoreHeatmapResponse:
        grid_size = max(1, grid_size)
        events = self.db.scalars(
            select(Event)
            .where(Event.store_id == store_id)
            .where(Event.hotspot_x.is_not(None))
            .where(Event.hotspot_y.is_not(None))
            .order_by(Event.timestamp)
        ).all()

        buckets: dict[tuple[int, int], HeatmapBucket] = defaultdict(HeatmapBucket)
        for event in events:
            x = _clamp(float(event.hotspot_x or 0.0))
            y = _clamp(float(event.hotspot_y or 0.0))
            bucket_x = min(grid_size - 1, int(x * grid_size))
            bucket_y = min(grid_size - 1, int(y * grid_size))
            buckets[(bucket_x, bucket_y)].add(x, y)

        max_count = max((bucket.count for bucket in buckets.values()), default=0)
        points = [
            HeatmapPoint(
                x=round(bucket.center_x, 4),
                y=round(bucket.center_y, 4),
                intensity=round(bucket.count / max_count, 4) if max_count else 0.0,
                count=bucket.count,
            )
            for _, bucket in sorted(
                buckets.items(),
                key=lambda item: (item[0][1], item[0][0]),
            )
        ]

        return StoreHeatmapResponse(
            store_id=store_id,
            layout_image_url=self.layout_image_url(store_id),
            grid_size=grid_size,
            total_events=len(events),
            point_count=len(points),
            max_count=max_count,
            data_confidence=self._data_confidence(store_id),
            points=points,
        )

    def _data_confidence(self, store_id: str) -> str:
        session_count = int(
            self.db.scalar(select(func.count(VisitSession.id)).where(VisitSession.store_id == store_id)) or 0
        )
        return "HIGH" if session_count >= 20 else "LOW"

    def active_map(self, store_id: str) -> Map | None:
        """The store's current layout asset, if one has been uploaded through
        onboarding (P7). Replaces the old hardcoded ``STORE_LAYOUTS`` dict --
        a store with no uploaded map simply has no layout image, rather than
        the engine only knowing about two specific stores."""
        return self.db.execute(
            select(Map).where(Map.store_id == store_id, Map.is_active.is_(True)).order_by(Map.created_at.desc())
        ).scalars().first()

    def layout_image_path(self, store_id: str):
        map_row = self.active_map(store_id)
        if map_row is None:
            return None
        path = resolve_relative_path(map_row.file_path)
        return path if path.is_file() else None

    def layout_image_url(self, store_id: str) -> str | None:
        map_row = self.active_map(store_id)
        if map_row is None:
            return None
        return f"/stores/{store_id}/config/maps/{map_row.id}/file"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
