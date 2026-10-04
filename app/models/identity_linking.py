import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow
from app.models.enums import IdentityLinkReason


class CameraAdjacency(Base):
    """P10.1: a store-configured, directional statement that a visitor last
    seen on ``from_camera_id`` may plausibly appear next on ``to_camera_id``
    within ``[min_transit_seconds, max_transit_seconds]``.

    Deliberately explicit and operator-authored, never inferred -- the same
    stance ``CameraCoverage``'s docstring takes for camera-frame geometry (no
    calibration/homography, just human-declared configuration; see
    app.models.spatial). A store with no ``CameraAdjacency`` rows has
    cross-camera identity linking fully inert:
    ``IdentityLinkingService.evaluate_candidates`` has nothing to evaluate,
    by construction, not by a special-cased guard.

    A pair is directional (``from_camera_id`` -> ``to_camera_id``) rather
    than symmetric -- a real store's entry-to-zone transit time need not
    equal its zone-to-entry one, and forcing a single symmetric row would
    conflate the two. A bidirectional relationship is just two rows.
    """

    __tablename__ = "camera_adjacency"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    from_camera_id: Mapped[str] = mapped_column(ForeignKey("camera.id"), index=True)
    to_camera_id: Mapped[str] = mapped_column(ForeignKey("camera.id"), index=True)
    min_transit_seconds: Mapped[float] = mapped_column(Float)
    max_transit_seconds: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        UniqueConstraint("store_id", "from_camera_id", "to_camera_id", name="uq_camera_adjacency_pair"),
    )


class IdentityLinkCandidate(Base):
    """P10.1: an auditable, proposal-only record of whether two camera-scoped
    ``TrackedEntity`` rows were considered a plausible cross-camera match for
    the same physical visitor -- and why or why not.

    This is deliberately NOT identity resolution. Nothing anywhere reads this
    table to decide what a ``TrackedEntity``/``VisitSession``/``Event`` means;
    no canonical-identity concept exists yet, no merge ever happens, and no
    other service (path analytics, queue, occupancy, correlation, anomaly,
    insights) is touched by this table's existence -- see
    ``IdentityLinkingService``'s module docstring for the full P10.1/P10.2
    boundary. A row with ``accepted=True`` records "these two entities are a
    strong pairwise candidate," never "these two entities are now the same
    visitor."

    ``entity_a_id`` is the entity as *last observed* (by any event, of any
    type) on the adjacency's ``from_camera_id``; ``entity_b_id`` is the
    entity as *first observed* (by any event, of any type) on the
    adjacency's ``to_camera_id``. This is deliberately not "the entity that
    EXITed camera A" / "the entity that ENTERed camera B" -- a formal
    EXIT/ENTRY event is not required and disappearing from camera A's view
    is never treated as proof of a physical exit (see
    ``IdentityLinkingService``).

    Every row this service ever produces -- accepted or rejected -- is kept,
    with an explicit ``reason``, so every decision is auditable after the
    fact. Pairs whose gap falls outside the adjacency's transit window are
    not persisted at all (see ``IdentityLinkingService._evaluate_adjacency``)
    -- they were never real candidates, so there is nothing to audit.
    """

    __tablename__ = "identity_link_candidate"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    adjacency_id: Mapped[str] = mapped_column(ForeignKey("camera_adjacency.id"), index=True)
    entity_a_id: Mapped[str] = mapped_column(ForeignKey("tracked_entity.id"), index=True)
    entity_b_id: Mapped[str] = mapped_column(ForeignKey("tracked_entity.id"), index=True)
    gap_seconds: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    accepted: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[IdentityLinkReason] = mapped_column()
    evaluated_at: Mapped[datetime] = mapped_column()

    __table_args__ = (
        UniqueConstraint(
            "store_id", "entity_a_id", "entity_b_id", "adjacency_id", name="uq_identity_link_candidate_pair"
        ),
    )
