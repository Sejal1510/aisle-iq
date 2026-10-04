# P10.1: proposal-only pairwise cross-camera identity-link candidate
# generation. IdentityLinkingService.evaluate_candidates never merges
# identities, never writes a canonical-identity pointer (none exists), and
# never chains an accepted A->B with an accepted B->C into a claim about A
# and C -- see test_pairwise_links_do_not_chain_transitively below, which is
# the direct replacement for the transitive/three-camera-chain test a prior,
# rejected design draft would have needed.
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import EventType, IdentityLinkReason
from app.models.event import Event
from app.models.identity_linking import CameraAdjacency, IdentityLinkCandidate
from app.models.store import Camera, Store
from app.models.tracking import TrackedEntity, VisitSession
from app.services.identity_linking_service import IdentityLinkingService

BASE_TIME = datetime(2026, 6, 1, 9, 0, 0)


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _store(db: Session, store_id: str) -> None:
    if db.get(Store, store_id) is None:
        db.add(Store(id=store_id, name=None))
        db.flush()


def _camera(db: Session, store_id: str, camera_id: str, *, role: str = "zone") -> None:
    _store(db, store_id)
    if db.get(Camera, camera_id) is None:
        db.add(Camera(id=camera_id, store_id=store_id, role=role))
        db.flush()


def _entity_with_event(
    db: Session, store_id: str, entity_id: str, camera_id: str, ts: datetime
) -> None:
    """A camera-scoped TrackedEntity with exactly one Event on ``camera_id``
    at ``ts`` -- enough for it to be found as both "last seen" and "first
    seen" on that camera, matching a real single-appearance track."""
    _store(db, store_id)
    if db.get(TrackedEntity, entity_id) is None:
        db.add(TrackedEntity(id=entity_id, store_id=store_id, is_staff=False))
        db.flush()
    session = VisitSession(tracked_entity_id=entity_id, store_id=store_id, entry_time=ts)
    db.add(session)
    db.flush()
    db.add(
        Event(
            session_id=session.id,
            tracked_entity_id=entity_id,
            store_id=store_id,
            camera_id=camera_id,
            event_type=EventType.ZONE_ENTERED,
            timestamp=ts,
        )
    )
    db.flush()


def _adjacency(
    db: Session,
    store_id: str,
    from_camera_id: str,
    to_camera_id: str,
    *,
    min_transit_seconds: float,
    max_transit_seconds: float,
) -> CameraAdjacency:
    adjacency = CameraAdjacency(
        store_id=store_id,
        from_camera_id=from_camera_id,
        to_camera_id=to_camera_id,
        min_transit_seconds=min_transit_seconds,
        max_transit_seconds=max_transit_seconds,
    )
    db.add(adjacency)
    db.flush()
    return adjacency


def _candidates(db: Session, store_id: str) -> list[IdentityLinkCandidate]:
    return list(
        db.scalars(select(IdentityLinkCandidate).where(IdentityLinkCandidate.store_id == store_id))
    )


# ---------------------------------------------------------------------------
# Accepted candidate + confidence formula
# ---------------------------------------------------------------------------


def test_unique_mutual_candidate_at_window_midpoint_is_accepted(db_session: Session) -> None:
    store_id = "ST_LINK_ACCEPT"
    _camera(db_session, store_id, "CAM_A", role="entry")
    _camera(db_session, store_id, "CAM_B", role="zone")
    _adjacency(db_session, store_id, "CAM_A", "CAM_B", min_transit_seconds=60, max_transit_seconds=180)

    _entity_with_event(db_session, store_id, "ENT_A1", "CAM_A", BASE_TIME)
    _entity_with_event(db_session, store_id, "ENT_B1", "CAM_B", BASE_TIME + timedelta(seconds=120))
    db_session.commit()

    results = IdentityLinkingService(db_session).evaluate_candidates(store_id)

    assert len(results) == 1
    candidate = results[0]
    assert candidate.entity_a_id == "ENT_A1"
    assert candidate.entity_b_id == "ENT_B1"
    assert candidate.gap_seconds == 120.0
    assert candidate.confidence == pytest.approx(1.0)
    assert candidate.accepted is True
    assert candidate.reason == IdentityLinkReason.ACCEPTED


def test_confidence_falls_off_linearly_from_window_midpoint(db_session: Session) -> None:
    store_id = "ST_LINK_CONFIDENCE"
    _camera(db_session, store_id, "CAM_A", role="entry")
    _camera(db_session, store_id, "CAM_B", role="zone")
    # midpoint = 120, half_width = 60 -- a gap of 105 is 15s off the
    # midpoint, so confidence = 1 - 15/60 = 0.75.
    _adjacency(db_session, store_id, "CAM_A", "CAM_B", min_transit_seconds=60, max_transit_seconds=180)

    _entity_with_event(db_session, store_id, "ENT_A1", "CAM_A", BASE_TIME)
    _entity_with_event(db_session, store_id, "ENT_B1", "CAM_B", BASE_TIME + timedelta(seconds=105))
    db_session.commit()

    results = IdentityLinkingService(db_session).evaluate_candidates(store_id)

    assert len(results) == 1
    assert results[0].confidence == pytest.approx(0.75)
    assert results[0].accepted is True


def test_gap_near_window_edge_is_below_threshold(db_session: Session) -> None:
    store_id = "ST_LINK_BELOW_THRESHOLD"
    _camera(db_session, store_id, "CAM_A", role="entry")
    _camera(db_session, store_id, "CAM_B", role="zone")
    # midpoint = 120, half_width = 60 -- a gap of 65 is 55s off the
    # midpoint, so confidence = 1 - 55/60 ~= 0.083, below the 0.5 default
    # threshold despite being a unique, mutual, in-window candidate.
    _adjacency(db_session, store_id, "CAM_A", "CAM_B", min_transit_seconds=60, max_transit_seconds=180)

    _entity_with_event(db_session, store_id, "ENT_A1", "CAM_A", BASE_TIME)
    _entity_with_event(db_session, store_id, "ENT_B1", "CAM_B", BASE_TIME + timedelta(seconds=65))
    db_session.commit()

    results = IdentityLinkingService(db_session).evaluate_candidates(store_id)

    assert len(results) == 1
    assert results[0].accepted is False
    assert results[0].reason == IdentityLinkReason.BELOW_THRESHOLD
    assert results[0].confidence < 0.5


# ---------------------------------------------------------------------------
# Out-of-window gaps: no candidate persisted at all
# ---------------------------------------------------------------------------


def test_gap_too_short_produces_no_candidate(db_session: Session) -> None:
    store_id = "ST_LINK_TOO_SHORT"
    _camera(db_session, store_id, "CAM_A", role="entry")
    _camera(db_session, store_id, "CAM_B", role="zone")
    _adjacency(db_session, store_id, "CAM_A", "CAM_B", min_transit_seconds=60, max_transit_seconds=180)

    _entity_with_event(db_session, store_id, "ENT_A1", "CAM_A", BASE_TIME)
    _entity_with_event(db_session, store_id, "ENT_B1", "CAM_B", BASE_TIME + timedelta(seconds=30))
    db_session.commit()

    results = IdentityLinkingService(db_session).evaluate_candidates(store_id)

    assert results == []
    assert _candidates(db_session, store_id) == []


def test_gap_too_long_produces_no_candidate(db_session: Session) -> None:
    store_id = "ST_LINK_TOO_LONG"
    _camera(db_session, store_id, "CAM_A", role="entry")
    _camera(db_session, store_id, "CAM_B", role="zone")
    _adjacency(db_session, store_id, "CAM_A", "CAM_B", min_transit_seconds=60, max_transit_seconds=180)

    _entity_with_event(db_session, store_id, "ENT_A1", "CAM_A", BASE_TIME)
    _entity_with_event(db_session, store_id, "ENT_B1", "CAM_B", BASE_TIME + timedelta(seconds=400))
    db_session.commit()

    results = IdentityLinkingService(db_session).evaluate_candidates(store_id)

    assert results == []
    assert _candidates(db_session, store_id) == []


def test_negative_gap_produces_no_candidate(db_session: Session) -> None:
    """B observed *before* A -- the wrong direction for this adjacency --
    must never be accepted, regardless of window width."""
    store_id = "ST_LINK_NEGATIVE_GAP"
    _camera(db_session, store_id, "CAM_A", role="entry")
    _camera(db_session, store_id, "CAM_B", role="zone")
    _adjacency(db_session, store_id, "CAM_A", "CAM_B", min_transit_seconds=0, max_transit_seconds=180)

    _entity_with_event(db_session, store_id, "ENT_A1", "CAM_A", BASE_TIME)
    _entity_with_event(db_session, store_id, "ENT_B1", "CAM_B", BASE_TIME - timedelta(seconds=30))
    db_session.commit()

    results = IdentityLinkingService(db_session).evaluate_candidates(store_id)

    assert results == []
    assert _candidates(db_session, store_id) == []


# ---------------------------------------------------------------------------
# Ambiguity rejection
# ---------------------------------------------------------------------------


def test_ambiguous_on_a_side_rejects_both_candidates(db_session: Session) -> None:
    store_id = "ST_LINK_AMBIGUOUS_A"
    _camera(db_session, store_id, "CAM_A", role="entry")
    _camera(db_session, store_id, "CAM_B", role="zone")
    _adjacency(db_session, store_id, "CAM_A", "CAM_B", min_transit_seconds=60, max_transit_seconds=180)

    _entity_with_event(db_session, store_id, "ENT_A1", "CAM_A", BASE_TIME)
    _entity_with_event(db_session, store_id, "ENT_B1", "CAM_B", BASE_TIME + timedelta(seconds=100))
    _entity_with_event(db_session, store_id, "ENT_B2", "CAM_B", BASE_TIME + timedelta(seconds=140))
    db_session.commit()

    results = IdentityLinkingService(db_session).evaluate_candidates(store_id)

    assert len(results) == 2
    assert {r.entity_b_id for r in results} == {"ENT_B1", "ENT_B2"}
    for candidate in results:
        assert candidate.entity_a_id == "ENT_A1"
        assert candidate.accepted is False
        assert candidate.reason == IdentityLinkReason.AMBIGUOUS


def test_ambiguous_on_b_side_rejects_both_candidates(db_session: Session) -> None:
    store_id = "ST_LINK_AMBIGUOUS_B"
    _camera(db_session, store_id, "CAM_A", role="entry")
    _camera(db_session, store_id, "CAM_B", role="zone")
    _adjacency(db_session, store_id, "CAM_A", "CAM_B", min_transit_seconds=60, max_transit_seconds=180)

    _entity_with_event(db_session, store_id, "ENT_A1", "CAM_A", BASE_TIME)
    _entity_with_event(db_session, store_id, "ENT_A2", "CAM_A", BASE_TIME + timedelta(seconds=10))
    _entity_with_event(db_session, store_id, "ENT_B1", "CAM_B", BASE_TIME + timedelta(seconds=120))
    db_session.commit()

    results = IdentityLinkingService(db_session).evaluate_candidates(store_id)

    assert len(results) == 2
    assert {r.entity_a_id for r in results} == {"ENT_A1", "ENT_A2"}
    for candidate in results:
        assert candidate.entity_b_id == "ENT_B1"
        assert candidate.accepted is False
        assert candidate.reason == IdentityLinkReason.AMBIGUOUS


# ---------------------------------------------------------------------------
# No configuration -> fully inert
# ---------------------------------------------------------------------------


def test_no_adjacency_configured_produces_no_candidates(db_session: Session) -> None:
    store_id = "ST_LINK_NO_ADJACENCY"
    _camera(db_session, store_id, "CAM_A", role="entry")
    _camera(db_session, store_id, "CAM_B", role="zone")

    _entity_with_event(db_session, store_id, "ENT_A1", "CAM_A", BASE_TIME)
    _entity_with_event(db_session, store_id, "ENT_B1", "CAM_B", BASE_TIME + timedelta(seconds=120))
    db_session.commit()

    results = IdentityLinkingService(db_session).evaluate_candidates(store_id)

    assert results == []
    assert _candidates(db_session, store_id) == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_repeated_evaluation_is_idempotent(db_session: Session) -> None:
    store_id = "ST_LINK_IDEMPOTENT"
    _camera(db_session, store_id, "CAM_A", role="entry")
    _camera(db_session, store_id, "CAM_B", role="zone")
    _adjacency(db_session, store_id, "CAM_A", "CAM_B", min_transit_seconds=60, max_transit_seconds=180)

    _entity_with_event(db_session, store_id, "ENT_A1", "CAM_A", BASE_TIME)
    _entity_with_event(db_session, store_id, "ENT_B1", "CAM_B", BASE_TIME + timedelta(seconds=120))
    db_session.commit()

    service = IdentityLinkingService(db_session)
    first_run = service.evaluate_candidates(store_id)
    db_session.commit()
    second_run = service.evaluate_candidates(store_id)
    db_session.commit()

    assert len(first_run) == 1
    assert len(second_run) == 1
    assert first_run[0].id == second_run[0].id
    assert len(_candidates(db_session, store_id)) == 1


# ---------------------------------------------------------------------------
# No transitive/multi-hop propagation (replaces the rejected chain-canonical
# design entirely -- see this module's top-of-file note)
# ---------------------------------------------------------------------------


def test_pairwise_links_do_not_chain_transitively(db_session: Session) -> None:
    store_id = "ST_LINK_NO_TRANSITIVITY"
    _camera(db_session, store_id, "CAM_A", role="entry")
    _camera(db_session, store_id, "CAM_B", role="zone")
    _camera(db_session, store_id, "CAM_C", role="billing")
    _adjacency(db_session, store_id, "CAM_A", "CAM_B", min_transit_seconds=60, max_transit_seconds=180)
    _adjacency(db_session, store_id, "CAM_B", "CAM_C", min_transit_seconds=60, max_transit_seconds=180)

    entry_time = BASE_TIME
    zone_time = BASE_TIME + timedelta(seconds=120)
    billing_time = zone_time + timedelta(seconds=120)

    _entity_with_event(db_session, store_id, "ENT_A1", "CAM_A", entry_time)
    _entity_with_event(db_session, store_id, "ENT_B1", "CAM_B", zone_time)
    _entity_with_event(db_session, store_id, "ENT_C1", "CAM_C", billing_time)
    db_session.commit()

    results = IdentityLinkingService(db_session).evaluate_candidates(store_id)

    accepted = [r for r in results if r.accepted]
    assert len(accepted) == 2
    pairs = {(r.entity_a_id, r.entity_b_id) for r in accepted}
    assert pairs == {("ENT_A1", "ENT_B1"), ("ENT_B1", "ENT_C1")}

    # The direct assertion this phase exists to prove: no row, of any kind,
    # relates ENT_A1 and ENT_C1 -- an accepted A->B plus an accepted B->C
    # never implies or produces anything about A and C.
    a_to_c_or_c_to_a = [
        r
        for r in _candidates(db_session, store_id)
        if {r.entity_a_id, r.entity_b_id} == {"ENT_A1", "ENT_C1"}
    ]
    assert a_to_c_or_c_to_a == []


# ---------------------------------------------------------------------------
# Multi-store isolation
# ---------------------------------------------------------------------------


def test_multiple_stores_remain_isolated(db_session: Session) -> None:
    store_1, store_2 = "ST_LINK_ISO_1", "ST_LINK_ISO_2"
    _camera(db_session, store_1, "CAM_ISO1_A", role="entry")
    _camera(db_session, store_1, "CAM_ISO1_B", role="zone")
    _camera(db_session, store_2, "CAM_ISO2_A", role="entry")
    _camera(db_session, store_2, "CAM_ISO2_B", role="zone")
    _adjacency(db_session, store_1, "CAM_ISO1_A", "CAM_ISO1_B", min_transit_seconds=60, max_transit_seconds=180)
    _adjacency(db_session, store_2, "CAM_ISO2_A", "CAM_ISO2_B", min_transit_seconds=60, max_transit_seconds=180)

    _entity_with_event(db_session, store_1, "ENT_ISO1_A1", "CAM_ISO1_A", BASE_TIME)
    _entity_with_event(db_session, store_1, "ENT_ISO1_B1", "CAM_ISO1_B", BASE_TIME + timedelta(seconds=120))
    _entity_with_event(db_session, store_2, "ENT_ISO2_A1", "CAM_ISO2_A", BASE_TIME)
    _entity_with_event(db_session, store_2, "ENT_ISO2_B1", "CAM_ISO2_B", BASE_TIME + timedelta(seconds=120))
    db_session.commit()

    service = IdentityLinkingService(db_session)
    store_1_results = service.evaluate_candidates(store_1)
    db_session.commit()

    assert len(store_1_results) == 1
    assert store_1_results[0].store_id == store_1
    # Evaluating store 1 must not have touched store 2 at all.
    assert _candidates(db_session, store_2) == []

    store_2_results = service.evaluate_candidates(store_2)
    db_session.commit()

    assert len(store_2_results) == 1
    assert store_2_results[0].store_id == store_2
    # Evaluating store 2 must not have changed store 1's already-computed
    # candidates.
    assert len(_candidates(db_session, store_1)) == 1
    assert _candidates(db_session, store_1)[0].id == store_1_results[0].id
