from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from math import hypot

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import EventType
from app.models.event import Event
from app.models.tracking import TrackedEntity, VisitSession

STAFF_ZONE_KEYWORDS = ("staff", "boh", "back", "office", "stock", "storage", "employee")
BILLING_ZONE_KEYWORDS = ("billing", "cash", "checkout", "queue", "counter")


@dataclass(frozen=True)
class StaffInference:
    entity_id: str
    confidence_score: float
    reason: str


@dataclass(frozen=True)
class GroupInference:
    group_id: str
    member_ids: tuple[str, ...]


@dataclass(frozen=True)
class VisitorInferenceResult:
    staff: list[StaffInference]
    groups: list[GroupInference]


@dataclass
class SessionProfile:
    session: VisitSession
    entity: TrackedEntity
    events: list[Event]

    @property
    def start(self):
        return self.session.entry_time

    @property
    def end(self):
        return self.session.exit_time or self.session.entry_time

    @property
    def duration_seconds(self) -> int:
        if self.session.dwell_seconds is not None:
            return self.session.dwell_seconds
        return max(0, int((self.end - self.start).total_seconds()))

    @property
    def zone_path(self) -> tuple[str, ...]:
        return tuple(
            event.zone_id
            for event in self.events
            if event.zone_id and event.event_type in {EventType.ZONE_ENTERED, EventType.ZONE_EXITED}
        )

    @property
    def hotspots(self) -> list[tuple[float, float]]:
        return [
            (event.hotspot_x, event.hotspot_y)
            for event in self.events
            if event.hotspot_x is not None and event.hotspot_y is not None
        ]

    @property
    def billing_events(self) -> list[Event]:
        return [
            event
            for event in self.events
            if event.event_type in {EventType.QUEUE_COMPLETED, EventType.QUEUE_ABANDONED}
            or _matches_keywords(event.zone_id, BILLING_ZONE_KEYWORDS)
        ]


class VisitorInferenceService:
    """Infer staff and shopper groups from explainable retail behavior signals."""

    def __init__(
        self,
        db: Session,
        *,
        long_presence_seconds: int = 45 * 60,
        group_entry_window_seconds: int = 120,
        group_overlap_seconds: int = 180,
        group_hotspot_distance: float = 0.18,
        max_group_size: int = 5,
    ):
        self.db = db
        self.long_presence_seconds = long_presence_seconds
        self.group_entry_window = timedelta(seconds=group_entry_window_seconds)
        self.group_overlap_seconds = group_overlap_seconds
        self.group_hotspot_distance = group_hotspot_distance
        self.max_group_size = max_group_size

    def infer_store(self, store_id: str) -> VisitorInferenceResult:
        self.db.flush()
        profiles = self._profiles(store_id)
        staff = self._infer_staff(profiles)
        groups = self._infer_groups([profile for profile in profiles if not profile.entity.is_staff])
        self.db.flush()
        return VisitorInferenceResult(staff=staff, groups=groups)

    def _infer_staff(self, profiles: list[SessionProfile]) -> list[StaffInference]:
        results: list[StaffInference] = []
        by_entity: dict[str, list[SessionProfile]] = {}
        for profile in profiles:
            by_entity.setdefault(profile.entity.id, []).append(profile)

        for entity_id, entity_profiles in by_entity.items():
            entity = entity_profiles[0].entity
            score, reasons = self._staff_score(entity_profiles)
            if entity.is_staff:
                score = max(score, 1.0)
                reasons.insert(0, "source marked as staff")

            entity.staff_confidence_score = round(score, 4)
            entity.staff_inference_reason = ", ".join(dict.fromkeys(reasons)) or "customer behavior"
            entity.is_staff = score >= 0.65

            if entity.is_staff:
                results.append(
                    StaffInference(
                        entity_id=entity_id,
                        confidence_score=entity.staff_confidence_score,
                        reason=entity.staff_inference_reason,
                    )
                )

        return results

    def _staff_score(self, profiles: list[SessionProfile]) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []
        total_duration = sum(profile.duration_seconds for profile in profiles)
        repeated_sessions = len(profiles)
        staff_zone_events = sum(
            1
            for profile in profiles
            for event in profile.events
            if _matches_keywords(event.zone_id, STAFF_ZONE_KEYWORDS)
        )
        billing_events = sum(len(profile.billing_events) for profile in profiles)
        shopping_zone_events = sum(
            1
            for profile in profiles
            for event in profile.events
            if event.zone_id
            and not _matches_keywords(event.zone_id, STAFF_ZONE_KEYWORDS + BILLING_ZONE_KEYWORDS)
        )

        if staff_zone_events:
            score += min(0.55, 0.25 + staff_zone_events * 0.10)
            reasons.append("known staff zone presence")
        if total_duration >= self.long_presence_seconds:
            score += min(0.35, total_duration / (4 * self.long_presence_seconds) * 0.35)
            reasons.append("repeated long-duration presence")
        if repeated_sessions >= 3:
            score += 0.20
            reasons.append("repeated store presence")
        if billing_events >= 3 and shopping_zone_events == 0:
            score += 0.35
            reasons.append("billing-area worker behavior")
        elif billing_events >= 3:
            score += 0.15
            reasons.append("frequent billing-area behavior")

        return min(score, 1.0), reasons

    def _infer_groups(self, profiles: list[SessionProfile]) -> list[GroupInference]:
        for profile in profiles:
            if _is_inferred_group_id(profile.entity.group_id, profile.session.store_id):
                profile.entity.group_id = None
                profile.entity.group_size = None

        unassigned = sorted(profiles, key=lambda profile: (profile.start, profile.entity.id))
        groups: list[GroupInference] = []
        group_index = 1

        for profile in unassigned:
            if profile.entity.group_id is not None:
                continue

            members = [profile]
            for candidate in unassigned:
                if candidate.entity.id == profile.entity.id or candidate.entity.group_id is not None:
                    continue
                if self._same_group(profile, candidate):
                    members.append(candidate)
                if len(members) >= self.max_group_size:
                    break

            unique_members = {member.entity.id: member for member in members}
            if len(unique_members) < 2:
                continue

            group_id = f"{profile.session.store_id}-GROUP-{group_index:03d}"
            group_index += 1
            member_ids = tuple(sorted(unique_members))
            group_size = len(member_ids)
            for member in unique_members.values():
                member.entity.group_id = group_id
                member.entity.group_size = group_size
            groups.append(GroupInference(group_id=group_id, member_ids=member_ids))

        return groups

    def _same_group(self, left: SessionProfile, right: SessionProfile) -> bool:
        entry_close = abs(left.start - right.start) <= self.group_entry_window
        overlap = _overlap_seconds(left.start, left.end, right.start, right.end)
        path_score = _path_similarity(left.zone_path, right.zone_path)
        hotspot_close = _hotspot_close(left.hotspots, right.hotspots, self.group_hotspot_distance)

        score = 0
        if entry_close:
            score += 1
        if overlap >= self.group_overlap_seconds:
            score += 1
        if path_score >= 0.5:
            score += 1
        if hotspot_close:
            score += 1

        return score >= 3

    def _profiles(self, store_id: str) -> list[SessionProfile]:
        sessions = list(
            self.db.scalars(
                select(VisitSession)
                .join(TrackedEntity)
                .where(VisitSession.store_id == store_id)
                .order_by(VisitSession.entry_time, VisitSession.id)
            )
        )
        if not sessions:
            return []

        events_by_session: dict[str, list[Event]] = {session.id: [] for session in sessions}
        events = self.db.scalars(
            select(Event)
            .where(Event.session_id.in_(events_by_session))
            .order_by(Event.session_id, Event.timestamp)
        ).all()
        for event in events:
            events_by_session.setdefault(event.session_id, []).append(event)

        return [
            SessionProfile(
                session=session,
                entity=session.tracked_entity,
                events=events_by_session.get(session.id, []),
            )
            for session in sessions
        ]


def _matches_keywords(value: str | None, keywords: tuple[str, ...]) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return any(keyword in lowered for keyword in keywords)


def _overlap_seconds(left_start, left_end, right_start, right_end) -> int:
    latest_start = max(left_start, right_start)
    earliest_end = min(left_end, right_end)
    return max(0, int((earliest_end - latest_start).total_seconds()))


def _path_similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    if not left or not right:
        return 0.0
    left_set = set(left)
    right_set = set(right)
    return len(left_set & right_set) / len(left_set | right_set)


def _hotspot_close(
    left: list[tuple[float, float]],
    right: list[tuple[float, float]],
    threshold: float,
) -> bool:
    if not left or not right:
        return False
    return any(hypot(left_x - right_x, left_y - right_y) <= threshold for left_x, left_y in left for right_x, right_y in right)


def _is_inferred_group_id(group_id: str | None, store_id: str) -> bool:
    return bool(group_id and group_id.startswith(f"{store_id}-GROUP-"))
