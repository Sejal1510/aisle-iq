from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from pipeline.video.config import (
    CameraRole,
    EntryLine,
    Point,
    PolygonZone,
    VideoProcessingConfig,
)
from pipeline.video.tracking import TrackSnapshot


@dataclass
class QueueState:
    joined_at: datetime
    last_seen_at: datetime
    position_at_join: int
    hotspot: tuple[float, float]
    queue_event_id: str = field(default_factory=lambda: str(uuid4()))


class VideoEventGenerator:
    def __init__(self, config: VideoProcessingConfig):
        self.config = config
        self._entry_side_by_track: dict[str, bool] = {}
        self._last_exit_by_track: dict[str, datetime] = {}
        self._inside_zones_by_track: dict[tuple[str, str], bool] = {}
        self._active_queue_by_track: dict[str, QueueState] = {}

    def process_snapshot(self, snapshot: TrackSnapshot) -> list[dict]:
        if self.config.role == CameraRole.ENTRY:
            return self._process_entry_snapshot(snapshot)
        if self.config.role == CameraRole.ZONE:
            return self._process_zone_snapshot(snapshot)
        if self.config.role == CameraRole.BILLING:
            return self._process_billing_snapshot(snapshot)
        return []

    def finalize(self) -> list[dict]:
        events: list[dict] = []
        for track_id, state in list(self._active_queue_by_track.items()):
            events.append(self._queue_terminal_event(track_id, state, state.last_seen_at, completed=False))
            del self._active_queue_by_track[track_id]
        return events

    def _process_entry_snapshot(self, snapshot: TrackSnapshot) -> list[dict]:
        if self.config.entry_line is None:
            return []

        currently_inside = _is_inside_entry_line(snapshot.normalized_footpoint, self.config.entry_line)
        previously_inside = self._entry_side_by_track.get(snapshot.track_id)
        self._entry_side_by_track[snapshot.track_id] = currently_inside

        if previously_inside is None or previously_inside == currently_inside:
            return []

        visitor_id = _video_identity(snapshot)
        if currently_inside:
            event_type = "REENTRY" if visitor_id in self._last_exit_by_track else "ENTRY"
        else:
            event_type = "EXIT"
            self._last_exit_by_track[visitor_id] = snapshot.timestamp

        return [self._canonical_event(snapshot=snapshot, event_type=event_type, visitor_id=visitor_id)]

    def _process_zone_snapshot(self, snapshot: TrackSnapshot) -> list[dict]:
        events: list[dict] = []
        footpoint = _point_from_tuple(snapshot.normalized_footpoint)

        for zone in self.config.zones:
            key = (snapshot.track_id, zone.id)
            inside = _point_in_polygon(footpoint, zone.polygon)
            was_inside = self._inside_zones_by_track.get(key, False)
            self._inside_zones_by_track[key] = inside

            if inside == was_inside:
                continue

            events.append(
                self._zone_event(
                    snapshot=snapshot,
                    zone=zone,
                    event_type="zone_entered" if inside else "zone_exited",
                )
            )

        return events

    def _process_billing_snapshot(self, snapshot: TrackSnapshot) -> list[dict]:
        queue_zone = self._queue_zone()
        if queue_zone is None:
            return []

        footpoint = _point_from_tuple(snapshot.normalized_footpoint)
        inside_queue = _point_in_polygon(footpoint, queue_zone.polygon)
        state = self._active_queue_by_track.get(snapshot.track_id)

        if inside_queue:
            if state is None:
                new_state = QueueState(
                    joined_at=snapshot.timestamp,
                    last_seen_at=snapshot.timestamp,
                    position_at_join=len(self._active_queue_by_track) + 1,
                    hotspot=snapshot.normalized_footpoint,
                )
                self._active_queue_by_track[snapshot.track_id] = new_state
                return [self._queue_join_event(snapshot, new_state)]

            state.last_seen_at = snapshot.timestamp
            state.hotspot = snapshot.normalized_footpoint
            return []

        if state is None:
            return []

        del self._active_queue_by_track[snapshot.track_id]
        dwell_seconds = int((snapshot.timestamp - state.joined_at).total_seconds())
        if dwell_seconds < self.config.queue_abandonment_seconds:
            return []

        completed = dwell_seconds >= self.config.queue_completion_seconds
        return [self._queue_terminal_event(snapshot.track_id, state, snapshot.timestamp, completed=completed)]

    def _zone_event(self, *, snapshot: TrackSnapshot, zone: PolygonZone, event_type: str) -> dict:
        hotspot_x, hotspot_y = snapshot.normalized_footpoint
        return {
            "event_id": str(uuid4()),
            "store_id": snapshot.store_id,
            "camera_id": snapshot.camera_id,
            "visitor_id": _video_identity(snapshot),
            "event_type": "ZONE_ENTER" if event_type == "zone_entered" else "ZONE_EXIT",
            "zone_id": zone.id,
            "timestamp": snapshot.timestamp.isoformat(),
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": round(snapshot.confidence, 4),
            "metadata": {
                "legacy_event_type": event_type,
                "sku_zone": zone.name,
                "zone_type": zone.type,
                "is_revenue_zone": "Yes" if zone.is_revenue_zone else "No",
                "hotspot_x": round(hotspot_x, 4),
                "hotspot_y": round(hotspot_y, 4),
                "queue_depth": None,
                "session_seq": None,
            },
        }

    def _queue_join_event(self, snapshot: TrackSnapshot, state: QueueState) -> dict:
        """Emitted the moment a track's footpoint enters the queue polygon.

        This is a distinct lifecycle event from the terminal COMPLETE/ABANDON event
        emitted later in ``_queue_terminal_event`` — the two share ``queue_event_id``
        so they can be correlated as the same queue visit.
        """
        queue_zone = self._queue_zone()
        hotspot_x, hotspot_y = state.hotspot
        return {
            "event_id": str(uuid4()),
            "store_id": snapshot.store_id,
            "camera_id": snapshot.camera_id,
            "visitor_id": _video_identity(snapshot),
            "event_type": "BILLING_QUEUE_JOIN",
            "timestamp": state.joined_at.isoformat(),
            "zone_id": queue_zone.id if queue_zone else f"{self.config.camera_id}_QUEUE",
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": round(snapshot.confidence, 4),
            "metadata": {
                "queue_event_id": state.queue_event_id,
                "legacy_event_type": "queue_join",
                "queue_depth": state.position_at_join,
                "queue_join_ts": state.joined_at.isoformat(),
                "sku_zone": queue_zone.name if queue_zone else "Billing Queue",
                "zone_type": queue_zone.type if queue_zone else "BILLING",
                "is_revenue_zone": "Yes",
                "hotspot_x": round(hotspot_x, 4),
                "hotspot_y": round(hotspot_y, 4),
                "session_seq": None,
            },
        }

    def _queue_terminal_event(self, track_id: str, state: QueueState, exit_ts: datetime, *, completed: bool) -> dict:
        """Emitted when a track leaves the queue polygon, closing out the visit opened
        by ``_queue_join_event``.

        ``wait_seconds`` is always ``exit_ts - state.joined_at`` — the only two
        timestamps the pipeline actually observes. There is no signal for when
        service started, so no served-time is fabricated; ``queue_served_ts`` is
        left ``None``.
        """
        queue_zone = self._queue_zone()
        wait_seconds = max(0, int((exit_ts - state.joined_at).total_seconds()))
        hotspot_x, hotspot_y = state.hotspot
        return {
            "event_id": str(uuid4()),
            "store_id": self.config.store_id,
            "camera_id": self.config.camera_id,
            "visitor_id": f"{self.config.camera_id}:{track_id}",
            "event_type": "BILLING_QUEUE_COMPLETE" if completed else "BILLING_QUEUE_ABANDON",
            "timestamp": exit_ts.isoformat(),
            "zone_id": queue_zone.id if queue_zone else f"{self.config.camera_id}_QUEUE",
            "dwell_ms": wait_seconds * 1000,
            "is_staff": False,
            "confidence": 0.8,
            "metadata": {
                "queue_event_id": state.queue_event_id,
                "legacy_event_type": "queue_completed" if completed else "queue_abandoned",
                "queue_depth": state.position_at_join,
                "queue_join_ts": state.joined_at.isoformat(),
                "queue_served_ts": None,
                "queue_exit_ts": exit_ts.isoformat(),
                "wait_seconds": wait_seconds,
                "queue_position_at_join": state.position_at_join,
                "abandoned": not completed,
                "sku_zone": queue_zone.name if queue_zone else "Billing Queue",
                "zone_type": queue_zone.type if queue_zone else "BILLING",
                "is_revenue_zone": "Yes",
                "hotspot_x": round(hotspot_x, 4),
                "hotspot_y": round(hotspot_y, 4),
                "session_seq": None,
            },
        }

    def _canonical_event(
        self,
        *,
        snapshot: TrackSnapshot,
        event_type: str,
        visitor_id: str,
        zone_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        return {
            "event_id": str(uuid4()),
            "store_id": snapshot.store_id,
            "camera_id": snapshot.camera_id,
            "visitor_id": visitor_id,
            "event_type": event_type,
            "timestamp": snapshot.timestamp.isoformat(),
            "zone_id": zone_id,
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": round(snapshot.confidence, 4),
            "metadata": {
                "queue_depth": None,
                "session_seq": None,
                **(metadata or {}),
            },
        }

    def _queue_zone(self) -> PolygonZone | None:
        if self.config.queue_zone_id is None:
            return self.config.zones[0] if self.config.zones else None
        for zone in self.config.zones:
            if zone.id == self.config.queue_zone_id:
                return zone
        return None


def _video_identity(snapshot: TrackSnapshot) -> str:
    return f"{snapshot.camera_id}:{snapshot.track_id}"


def _point_from_tuple(value: tuple[float, float]) -> Point:
    return Point(x=value[0], y=value[1])


def _is_inside_entry_line(point: tuple[float, float], line: EntryLine) -> bool:
    value = point[0] if line.axis == "x" else point[1]
    if line.inside_greater_than_position:
        return value >= line.position
    return value <= line.position


def _point_in_polygon(point: Point, polygon: tuple[Point, ...]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i, current in enumerate(polygon):
        previous = polygon[j]
        intersects = (current.y > point.y) != (previous.y > point.y)
        if intersects:
            slope_x = (previous.x - current.x) * (point.y - current.y) / (previous.y - current.y) + current.x
            if point.x < slope_x:
                inside = not inside
        j = i
    return inside
