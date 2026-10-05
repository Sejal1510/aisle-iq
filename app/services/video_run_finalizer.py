"""End-of-video finalization for one P9 video-processing run.

A recorded video is finite: once its last frame has been processed, nobody
in it can generate another event. Two things only become knowable at that
point, and both are derived purely from what the tracker actually observed
(``pipeline.video.annotation.RunStats``) -- nothing here invents a
timestamp or a person:

1. Visit sessions still IN_PROGRESS for people seen in this video are closed
   at the moment each person was *last observed* by the camera. Before this,
   every track that never crossed an EXIT line stayed "in the store" forever
   (inflating current occupancy) and never contributed a dwell time.

2. Staff classification from operator configuration. A track is staff only
   when the camera has a staff-only area configured (a ZoneType.STAFF_AREA
   coverage polygon, e.g. the operator side of a till) and the person's
   footpoint was inside it for most of their sightings. Nothing else -- not
   how long someone stayed, not standing near the counter -- makes a person
   staff: a customer waiting at the till for two minutes is still a
   customer. With no staff area configured, nobody is classified as staff by
   this step. Staff entities are excluded from every customer metric.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import SessionStatus
from app.models.tracking import IdentityAlias, TrackedEntity, VisitSession

# A track is staff when at least this share of its sightings were inside a
# configured staff-only area -- most of the time, not a customer briefly
# reaching or stepping across the counter...
STAFF_AREA_MIN_SHARE = 0.6
# ...and at least this many analysed frames were inside it (~5 s at the
# pipeline's 2 fps), so a handful of frames is never enough evidence.
STAFF_AREA_MIN_FRAMES = 10

VIDEO_SOURCE_FIELD = "visitor_id"


@dataclass(frozen=True)
class FinalizationResult:
    sessions_closed: int
    staff_tracks: tuple[str, ...]


class VideoRunFinalizer:
    def __init__(self, db: Session):
        self.db = db

    def finalize(
        self,
        *,
        store_id: str,
        camera_id: str,
        track_last_seen: dict[str, datetime],
        track_frames: dict[str, int],
        track_staff_area_frames: dict[str, int],
    ) -> FinalizationResult:
        entity_by_identity = self._entities_for(store_id, camera_id, list(track_last_seen))

        staff_tracks: list[str] = []
        for identity, staff_frames in track_staff_area_frames.items():
            share = staff_frames / max(track_frames.get(identity, 0), staff_frames, 1)
            if staff_frames < STAFF_AREA_MIN_FRAMES or share < STAFF_AREA_MIN_SHARE:
                continue
            staff_tracks.append(identity)
            # A staff member who never produced an event has no entity and
            # so never reached any customer metric; there is nothing to mark.
            entity = entity_by_identity.get(identity)
            if entity is not None:
                entity.is_staff = True
                entity.staff_confidence_score = round(share, 4)
                entity.staff_inference_reason = f"in configured staff area for {share:.0%} of sightings"

        sessions_closed = 0
        for identity, entity in entity_by_identity.items():
            last_seen = track_last_seen[identity]
            open_sessions = self.db.scalars(
                select(VisitSession).where(
                    VisitSession.tracked_entity_id == entity.id,
                    VisitSession.store_id == store_id,
                    VisitSession.session_status == SessionStatus.IN_PROGRESS,
                )
            ).all()
            for session in open_sessions:
                exit_time = max(last_seen, session.entry_time)
                session.exit_time = exit_time
                session.dwell_seconds = int((exit_time - session.entry_time).total_seconds())
                session.session_status = SessionStatus.COMPLETED
                sessions_closed += 1

        self.db.flush()
        return FinalizationResult(sessions_closed=sessions_closed, staff_tracks=tuple(sorted(staff_tracks)))

    def _entities_for(self, store_id: str, camera_id: str, identities: list[str]) -> dict[str, TrackedEntity]:
        """Only tracks that actually produced an event have a TrackedEntity
        (a track that never crossed a line or entered a zone left no trace in
        the event store), so anything without an alias is simply skipped."""
        if not identities:
            return {}
        rows = self.db.execute(
            select(IdentityAlias.source_value, TrackedEntity)
            .join(TrackedEntity, TrackedEntity.id == IdentityAlias.tracked_entity_id)
            .where(
                IdentityAlias.store_id == store_id,
                IdentityAlias.camera_id == camera_id,
                IdentityAlias.source_field == VIDEO_SOURCE_FIELD,
                IdentityAlias.source_value.in_(identities),
            )
        ).all()
        return dict(rows)
