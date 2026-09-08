from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow
from app.models.enums import ZoneType


class Organization(Base):
    """The tenant boundary above Store. There is exactly one row in practice
    today (single-tenant deployment) -- this table exists so the hierarchy is
    representable in the schema and Store rows carry a real foreign key,
    without building any multi-tenant business logic or API surface around it
    yet. See ReferenceDataService for how a default organization is
    provisioned."""

    __tablename__ = "organization"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    stores: Mapped[list["Store"]] = relationship(back_populates="organization")


class Store(Base):
    """``organization_id`` is nullable by design, not an oversight: ingestion
    (``ReferenceDataService.ensure_store``) always assigns a real
    organization, but a nullable column means the many existing call sites
    that construct a ``Store`` directly (tests, the POS ingestion path, demo
    fixtures) do not all have to become organization-aware in this phase just
    to keep working. A NULL organization_id is valid under both SQLite and
    PostgreSQL foreign key semantics."""

    __tablename__ = "store"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(ForeignKey("organization.id"), index=True)
    name: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    organization: Mapped["Organization"] = relationship(back_populates="stores")
    cameras: Mapped[list["Camera"]] = relationship(back_populates="store", cascade="all, delete-orphan")
    zones: Mapped[list["Zone"]] = relationship(back_populates="store", cascade="all, delete-orphan")


class Camera(Base):
    """The processing-tuning columns below (``video_path`` through
    ``queue_abandonment_seconds``) are P7 additions: they are exactly the
    per-camera fields ``pipeline.video.config.VideoProcessingConfig`` already
    needed, moved from hardcoded Python literals
    (``pipeline/video/config.py``'s old ``default_video_configs()``) into
    persisted, store-specific configuration. All are nullable -- a camera
    that has not been fully configured for video processing yet (e.g. one
    only used for live viewing, or mid-onboarding) is still a valid Camera
    row; ``pipeline.video.config.load_video_configs_from_db`` skips cameras
    missing the fields it needs rather than failing the whole store.

    ``reference_image_path`` is an optional still image an operator can
    upload for this camera (not extracted from video -- see the P7 audit's
    dependency-discipline note) purely as a drawing backdrop for its
    ``CameraCoverage`` geometry in the onboarding UI.
    """

    __tablename__ = "camera"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    name: Mapped[str | None] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    video_path: Mapped[str | None] = mapped_column(String)
    reference_image_path: Mapped[str | None] = mapped_column(String)
    start_time: Mapped[datetime | None] = mapped_column()
    sample_fps: Mapped[float | None] = mapped_column(Float)
    confidence_threshold: Mapped[float | None] = mapped_column(Float)
    queue_completion_seconds: Mapped[int | None] = mapped_column(Integer)
    queue_abandonment_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    store: Mapped["Store"] = relationship(back_populates="cameras")


class Zone(Base):
    """``map_polygon_json`` (P7) is the zone's shape drawn on the store's
    ``Map`` -- purely for visualization/onboarding, e.g. rendering zone
    outlines over the layout image. It is independent of the camera-frame
    geometry a ``CameraCoverage`` row uses for actual point-in-polygon
    testing (``pipeline/video/events.py`` never reads this field); the two
    are associated only through sharing the same ``zone_id``, per the P7
    audit's conservative camera-to-map mapping decision. Nullable because a
    zone can exist (and already drive CV/analytics) before its map outline
    has been drawn.
    """

    __tablename__ = "zone"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    type: Mapped[ZoneType] = mapped_column()
    is_revenue_zone: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    map_polygon_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    store: Mapped["Store"] = relationship(back_populates="zones")
