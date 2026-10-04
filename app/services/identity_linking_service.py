"""P10.1: proposal-only cross-camera identity-link candidate generation.

Scope boundary (do not read past this file for more than it promises):
this service answers exactly one question per pair of camera-scoped
TrackedEntity rows -- "given the configured camera relationship, should
AisleIQ consider these two entities a plausible cross-camera match?" -- and
persists the answer, with an explicit reason, as an IdentityLinkCandidate
row. It never merges identities, never writes a canonical-identity pointer
(no such column exists yet -- see app.models.tracking.TrackedEntity), never
looks at more than two entities at a time (no transitive/multi-hop chaining:
an accepted A->B row and an accepted B->C row are two independent facts,
never combined into a claim about A and C), and is never called by any other
service in this codebase. PathAnalyticsService, QueueService,
OccupancyService, POS correlation, and the anomaly/insights services do not
know this table exists.

A later phase (P10.2, not implemented here) would be the one to introduce an
actual canonical-identity concept that *consumes* this table's accepted rows
-- deliberately deferred so that this phase's output can be reviewed and
validated against real footage first.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.enums import IdentityLinkReason
from app.models.event import Event
from app.models.identity_linking import CameraAdjacency, IdentityLinkCandidate

# Not a calibrated probability -- see _temporal_fit_confidence. A candidate
# is only ever "accepted" if it is also mutually unique (see
# _evaluate_adjacency); this threshold alone never accepts a candidate.
DEFAULT_CONFIDENCE_THRESHOLD = 0.7


@dataclass(frozen=True)
class CandidateEvaluationSummary:
    accepted: int
    ambiguous: int
    below_threshold: int

    @property
    def total(self) -> int:
        return self.accepted + self.ambiguous + self.below_threshold


class IdentityLinkingService:
    def __init__(self, db: Session):
        self.db = db

    def evaluate_candidates(
        self, store_id: str, *, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    ) -> list[IdentityLinkCandidate]:
        """Evaluate every CameraAdjacency configured for ``store_id`` and
        persist (insert or update) an IdentityLinkCandidate row for every
        pairwise candidate found -- accepted or rejected.

        A store with no CameraAdjacency rows returns an empty list without
        error: the feature is inert until an operator configures at least
        one adjacency, by construction, not by a special case here.

        Idempotent: re-running against unchanged Event/CameraAdjacency data
        re-derives and upserts the same rows (matched by the
        (store_id, entity_a_id, entity_b_id, adjacency_id) unique key)
        rather than creating duplicates.
        """
        adjacencies = list(
            self.db.scalars(select(CameraAdjacency).where(CameraAdjacency.store_id == store_id))
        )

        results: list[IdentityLinkCandidate] = []
        for adjacency in adjacencies:
            results.extend(self._evaluate_adjacency(adjacency, threshold=threshold))
        self.db.flush()
        return results

    def _evaluate_adjacency(
        self, adjacency: CameraAdjacency, *, threshold: float
    ) -> list[IdentityLinkCandidate]:
        # "Last/first observed" is deliberately by any Event on that camera,
        # of any event_type -- not specifically an EXIT or ENTRY event. A
        # track can disappear from a camera's view for reasons that have
        # nothing to do with physically leaving the store (occlusion, frame
        # gaps, the video simply ending), so treating a formal EXIT as a
        # prerequisite would silently miss real candidates and would falsely
        # imply that this service knows something about physical movement
        # that it does not.
        last_on_from = self._last_seen_by_entity(adjacency.store_id, adjacency.from_camera_id)
        first_on_to = self._first_seen_by_entity(adjacency.store_id, adjacency.to_camera_id)

        candidates_by_a: dict[str, list[str]] = {}
        candidates_by_b: dict[str, list[str]] = {}
        gap_by_pair: dict[tuple[str, str], float] = {}

        for entity_a_id, last_seen in last_on_from.items():
            for entity_b_id, first_seen in first_on_to.items():
                if entity_a_id == entity_b_id:
                    # A TrackedEntity's events are all on one camera under
                    # today's camera-scoped identity resolution (see
                    # EventIngestionService._resolve_tracked_entity), so this
                    # is unreachable except for a degenerate self-adjacency
                    # (from_camera_id == to_camera_id) -- skipped rather than
                    # treated as a candidate for itself either way.
                    continue

                gap_seconds = (first_seen - last_seen).total_seconds()
                if adjacency.min_transit_seconds <= gap_seconds <= adjacency.max_transit_seconds:
                    candidates_by_a.setdefault(entity_a_id, []).append(entity_b_id)
                    candidates_by_b.setdefault(entity_b_id, []).append(entity_a_id)
                    gap_by_pair[(entity_a_id, entity_b_id)] = gap_seconds

        rows: list[IdentityLinkCandidate] = []
        seen_pairs: set[tuple[str, str]] = set()
        for entity_a_id, b_candidates in candidates_by_a.items():
            for entity_b_id in b_candidates:
                pair = (entity_a_id, entity_b_id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                gap_seconds = gap_by_pair[pair]

                # Mutual uniqueness: A's only in-window candidate must be B,
                # AND B's only in-window candidate must be A. Either side
                # having more than one in-window match makes the pair
                # ambiguous -- reject it rather than guess.
                mutually_unique = (
                    len(candidates_by_a[entity_a_id]) == 1 and len(candidates_by_b[entity_b_id]) == 1
                )

                if not mutually_unique:
                    rows.append(
                        self._upsert(
                            adjacency,
                            entity_a_id,
                            entity_b_id,
                            gap_seconds=gap_seconds,
                            confidence=0.0,
                            accepted=False,
                            reason=IdentityLinkReason.AMBIGUOUS,
                        )
                    )
                    continue

                confidence = _temporal_fit_confidence(
                    gap_seconds, adjacency.min_transit_seconds, adjacency.max_transit_seconds
                )
                accepted = confidence >= threshold
                reason = IdentityLinkReason.ACCEPTED if accepted else IdentityLinkReason.BELOW_THRESHOLD
                rows.append(
                    self._upsert(
                        adjacency,
                        entity_a_id,
                        entity_b_id,
                        gap_seconds=gap_seconds,
                        confidence=confidence,
                        accepted=accepted,
                        reason=reason,
                    )
                )

        return rows

    def _upsert(
        self,
        adjacency: CameraAdjacency,
        entity_a_id: str,
        entity_b_id: str,
        *,
        gap_seconds: float,
        confidence: float,
        accepted: bool,
        reason: IdentityLinkReason,
    ) -> IdentityLinkCandidate:
        existing = self.db.execute(
            select(IdentityLinkCandidate).where(
                IdentityLinkCandidate.store_id == adjacency.store_id,
                IdentityLinkCandidate.entity_a_id == entity_a_id,
                IdentityLinkCandidate.entity_b_id == entity_b_id,
                IdentityLinkCandidate.adjacency_id == adjacency.id,
            )
        ).scalar_one_or_none()

        now = utcnow()
        if existing is not None:
            existing.gap_seconds = gap_seconds
            existing.confidence = confidence
            existing.accepted = accepted
            existing.reason = reason
            existing.evaluated_at = now
            return existing

        candidate = IdentityLinkCandidate(
            store_id=adjacency.store_id,
            adjacency_id=adjacency.id,
            entity_a_id=entity_a_id,
            entity_b_id=entity_b_id,
            gap_seconds=gap_seconds,
            confidence=confidence,
            accepted=accepted,
            reason=reason,
            evaluated_at=now,
        )
        self.db.add(candidate)
        self.db.flush()
        return candidate

    def _last_seen_by_entity(self, store_id: str, camera_id: str) -> dict[str, datetime]:
        rows = self.db.execute(
            select(Event.tracked_entity_id, func.max(Event.timestamp))
            .where(Event.store_id == store_id, Event.camera_id == camera_id)
            .group_by(Event.tracked_entity_id)
        ).all()
        return dict(rows)

    def _first_seen_by_entity(self, store_id: str, camera_id: str) -> dict[str, datetime]:
        rows = self.db.execute(
            select(Event.tracked_entity_id, func.min(Event.timestamp))
            .where(Event.store_id == store_id, Event.camera_id == camera_id)
            .group_by(Event.tracked_entity_id)
        ).all()
        return dict(rows)


def _temporal_fit_confidence(gap_seconds: float, min_transit_seconds: float, max_transit_seconds: float) -> float:
    """Deterministic, explainable confidence in [0, 1] -- not a calibrated
    probability. Peaks at 1.0 at the configured window's midpoint (treated as
    the "typical" transit time) and falls off linearly toward either edge,
    reaching 0.0 at the boundaries themselves. A degenerate window
    (min == max) always scores 1.0, since any in-window gap must equal it
    exactly.
    """
    midpoint = (min_transit_seconds + max_transit_seconds) / 2
    half_width = (max_transit_seconds - min_transit_seconds) / 2
    if half_width <= 0:
        return 1.0
    distance_from_midpoint = abs(gap_seconds - midpoint)
    return max(0.0, 1.0 - (distance_from_midpoint / half_width))
