# PROMPT: Prove the P9 video-event identity fix -- reprocessing the same
# uploaded video must not fan out into duplicate Event/TrackedEntity/
# VisitSession rows, while a different upload to the same camera must not be
# swallowed as a false duplicate.
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.enums import EventType
from app.models.event import Event
from app.models.raw_event import RawEvent
from app.models.tracking import TrackedEntity, VisitSession
from app.schemas.event import EventPayload
from app.services.event_ingestion_service import EventIngestionService
from pipeline.video.config import (
    CameraRole,
    EntryLine,
    Point,
    PolygonZone,
    VideoProcessingConfig,
)
from pipeline.video.events import VideoEventGenerator
from pipeline.video.tracking import TrackSnapshot

BASE_TIME = datetime(2026, 7, 1, 9, 0, 0)


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


def _snapshot(
    *,
    track_id: str = "1",
    store_id: str,
    camera_id: str,
    frame_index: int,
    timestamp: datetime,
    normalized_footpoint: tuple[float, float],
) -> TrackSnapshot:
    width = height = 100
    x = normalized_footpoint[0] * width
    y = normalized_footpoint[1] * height
    return TrackSnapshot(
        track_id=track_id,
        store_id=store_id,
        camera_id=camera_id,
        frame_index=frame_index,
        timestamp=timestamp,
        bbox_xyxy=(x - 5, y - 20, x + 5, y),
        confidence=0.9,
        footpoint=(x, y),
        frame_size=(width, height),
    )


def _entry_config(*, video_path: Path, store_id: str = "ST_VID", camera_id: str = "CAM_VID") -> VideoProcessingConfig:
    return VideoProcessingConfig(
        store_id=store_id,
        camera_id=camera_id,
        role=CameraRole.ENTRY,
        video_path=video_path,
        start_time=BASE_TIME,
        entry_line=EntryLine(axis="x", position=0.5, inside_greater_than_position=True),
    )


def _generate_entry_exit_events(config: VideoProcessingConfig) -> list[dict]:
    """Runs a fixed baseline -> ENTRY -> EXIT snapshot sequence through a
    fresh VideoEventGenerator for the given config -- standing in for "one
    full pass over a video's frames" without needing a real video file or
    tracker, the same substitution test_video_event_generation.py already
    makes."""
    generator = VideoEventGenerator(config)
    events: list[dict] = []
    events += generator.process_snapshot(
        _snapshot(
            store_id=config.store_id, camera_id=config.camera_id,
            frame_index=0, timestamp=BASE_TIME, normalized_footpoint=(0.25, 0.5),
        )
    )
    events += generator.process_snapshot(
        _snapshot(
            store_id=config.store_id, camera_id=config.camera_id,
            frame_index=2, timestamp=BASE_TIME + timedelta(seconds=1), normalized_footpoint=(0.65, 0.5),
        )
    )
    events += generator.process_snapshot(
        _snapshot(
            store_id=config.store_id, camera_id=config.camera_id,
            frame_index=4, timestamp=BASE_TIME + timedelta(seconds=2), normalized_footpoint=(0.35, 0.5),
        )
    )
    events += generator.finalize()
    return events


def _billing_config(*, video_path: Path, store_id: str = "ST_VID", camera_id: str = "CAM_BILL") -> VideoProcessingConfig:
    zone = PolygonZone(
        id="QUEUE_1",
        name="Queue",
        type="BILLING",
        is_revenue_zone=True,
        polygon=(Point(0.25, 0.25), Point(0.75, 0.25), Point(0.75, 0.75), Point(0.25, 0.75)),
    )
    return VideoProcessingConfig(
        store_id=store_id,
        camera_id=camera_id,
        role=CameraRole.BILLING,
        video_path=video_path,
        start_time=BASE_TIME,
        zones=(zone,),
        queue_zone_id="QUEUE_1",
        queue_completion_seconds=30,
        queue_abandonment_seconds=5,
    )


def _generate_queue_visit_events(config: VideoProcessingConfig) -> list[dict]:
    """One queue visit long enough to complete: [JOIN, COMPLETE]."""
    generator = VideoEventGenerator(config)
    events: list[dict] = []
    events += generator.process_snapshot(
        _snapshot(
            store_id=config.store_id, camera_id=config.camera_id,
            frame_index=0, timestamp=BASE_TIME, normalized_footpoint=(0.5, 0.5),
        )
    )
    events += generator.process_snapshot(
        _snapshot(
            store_id=config.store_id, camera_id=config.camera_id,
            frame_index=70, timestamp=BASE_TIME + timedelta(seconds=35), normalized_footpoint=(0.9, 0.9),
        )
    )
    return events


def _ingest_all(db_session: Session, events: list[dict]) -> None:
    adapter = TypeAdapter(EventPayload)
    service = EventIngestionService(db_session)
    for event in events:
        service.process_event(adapter.validate_python(event), run_inference=False)
    db_session.commit()


# --- A. Same video processed twice -----------------------------------------


def test_reprocessing_same_video_creates_no_duplicate_rows(db_session: Session) -> None:
    video_path = Path("data/videos/ST_VID/upload_a.mp4")
    first_run_events = _generate_entry_exit_events(_entry_config(video_path=video_path))
    second_run_events = _generate_entry_exit_events(_entry_config(video_path=video_path))

    _ingest_all(db_session, first_run_events)
    event_count_after_first = db_session.scalar(select(func.count()).select_from(Event))
    entity_count_after_first = db_session.scalar(select(func.count()).select_from(TrackedEntity))
    session_count_after_first = db_session.scalar(select(func.count()).select_from(VisitSession))

    _ingest_all(db_session, second_run_events)

    assert db_session.scalar(select(func.count()).select_from(Event)) == event_count_after_first
    assert db_session.scalar(select(func.count()).select_from(TrackedEntity)) == entity_count_after_first
    assert db_session.scalar(select(func.count()).select_from(VisitSession)) == session_count_after_first
    assert db_session.scalar(
        select(func.count()).select_from(Event).where(Event.event_type == EventType.REENTRY)
    ) == 0
    assert db_session.scalar(
        select(func.count()).select_from(RawEvent).where(RawEvent.validation_status == "duplicate")
    ) == len(second_run_events)


# --- B. Different video, same camera ----------------------------------------


def test_different_video_same_camera_is_not_treated_as_duplicate(db_session: Session) -> None:
    events_a = _generate_entry_exit_events(_entry_config(video_path=Path("data/videos/ST_VID/upload_a.mp4")))
    events_b = _generate_entry_exit_events(_entry_config(video_path=Path("data/videos/ST_VID/upload_b.mp4")))

    # Same camera, same track_id, same event types, same frame_index sequence
    # -- only the upload differs.
    assert [e["event_type"] for e in events_a] == [e["event_type"] for e in events_b]

    _ingest_all(db_session, events_a)
    count_after_a = db_session.scalar(select(func.count()).select_from(Event))
    _ingest_all(db_session, events_b)
    count_after_b = db_session.scalar(select(func.count()).select_from(Event))

    assert count_after_b == count_after_a + len(events_b)
    assert db_session.scalar(
        select(func.count()).select_from(RawEvent).where(RawEvent.validation_status == "duplicate")
    ) == 0


# --- C. event_id stability ---------------------------------------------------


def test_event_id_is_stable_across_reprocessing_and_differs_by_video() -> None:
    video_path = Path("data/videos/ST_VID/upload_a.mp4")
    events_first_run = _generate_entry_exit_events(_entry_config(video_path=video_path))
    events_rerun = _generate_entry_exit_events(_entry_config(video_path=video_path))
    events_other_video = _generate_entry_exit_events(
        _entry_config(video_path=Path("data/videos/ST_VID/upload_b.mp4"))
    )

    assert [e["event_id"] for e in events_first_run] == [e["event_id"] for e in events_rerun]
    assert [e["event_id"] for e in events_first_run] != [e["event_id"] for e in events_other_video]


# --- D. queue_event_id identity ---------------------------------------------


def test_queue_event_id_stable_for_same_video_and_differs_for_different_video() -> None:
    video_path = Path("data/videos/ST_VID/upload_a.mp4")
    join_a, complete_a = _generate_queue_visit_events(_billing_config(video_path=video_path))
    join_a2, complete_a2 = _generate_queue_visit_events(_billing_config(video_path=video_path))
    join_b, complete_b = _generate_queue_visit_events(
        _billing_config(video_path=Path("data/videos/ST_VID/upload_b.mp4"))
    )

    # JOIN and its own terminal event correlate within one run...
    assert join_a["metadata"]["queue_event_id"] == complete_a["metadata"]["queue_event_id"]
    # ...and reproduce identically on a rerun of the same video...
    assert join_a["metadata"]["queue_event_id"] == join_a2["metadata"]["queue_event_id"]
    assert complete_a["metadata"]["queue_event_id"] == complete_a2["metadata"]["queue_event_id"]
    # ...but do not correlate across two different uploads.
    assert join_a["metadata"]["queue_event_id"] != join_b["metadata"]["queue_event_id"]
    assert complete_a["metadata"]["queue_event_id"] != complete_b["metadata"]["queue_event_id"]
