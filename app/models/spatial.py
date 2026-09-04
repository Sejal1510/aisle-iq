import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


class Map(Base):
    """A retailer-uploaded store floor plan asset (P7).

    Stored as a plain file on disk (``file_path``, relative to the project
    root) plus enough metadata to render it and let an operator draw zone/
    coverage geometry on top of it in the onboarding UI. No parsing of the
    file's contents happens here -- ``width_px``/``height_px`` are supplied
    by the client (which already has the image loaded in a <canvas> to draw
    on) rather than computed server-side, specifically to avoid adding an
    image-processing dependency for a one-field convenience.

    A store may have multiple ``Map`` rows over time (e.g. a re-upload); only
    the most recent ``is_active`` row is used by ``HeatmapService`` and the
    onboarding UI's default view.
    """

    __tablename__ = "map"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    name: Mapped[str | None] = mapped_column(String)
    file_path: Mapped[str] = mapped_column(String)
    content_type: Mapped[str | None] = mapped_column(String)
    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CameraCoverage(Base):
    """The spatial correspondence between one camera's frame and the store's
    spatial model (P7) -- this is what lets ``pipeline/video`` keep doing a
    simple in-frame point-in-polygon/line-side test (see
    ``pipeline.video.config.load_video_configs_from_db``) while the store
    still has a real, persisted spatial configuration instead of Python
    literals.

    Deliberately conservative (see the P7 audit's Camera -> Map -> Zone
    Mapping section): this does NOT attempt camera calibration, homography,
    or any pixel-to-pixel transform between camera space and map space. It
    just stores the human-drawn camera-frame geometry that the video pipeline
    tests against, associated with the zone that geometry represents. The
    zone's own ``map_polygon_json`` (see ``Zone``) is what carries the
    corresponding map-space region; the two are linked through ``zone_id``,
    not through a computed transform. A future, more sophisticated
    calibration mechanism can be introduced by adding fields here without
    changing ``pipeline.video.events`` or any downstream analytics.

    ``zone_id`` is nullable because an ENTRY-role camera's coverage is an
    entry line, not a zone (matching ``VideoProcessingConfig.entry_line``,
    which today is independent of ``VideoProcessingConfig.zones``).
    """

    __tablename__ = "camera_coverage"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    camera_id: Mapped[str] = mapped_column(ForeignKey("camera.id"), index=True)
    zone_id: Mapped[str | None] = mapped_column(ForeignKey("zone.id"), index=True)
    geometry_kind: Mapped[str] = mapped_column(String)
    geometry_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    zone: Mapped["Zone"] = relationship()  # noqa: F821 -- Zone imported lazily by SQLAlchemy string reference


# Geometry kinds recognized in CameraCoverage.geometry_kind / Zone.map_polygon_json.
GEOMETRY_KIND_POLYGON = "polygon"
GEOMETRY_KIND_LINE = "line"
