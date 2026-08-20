# PROMPT: Generate tests for video-derived event generation across entry, zone, billing, JSONL, and REENTRY behavior.
# CHANGES MADE: Updated assertions for the canonical challenge event schema while preserving movement-state coverage.
import io
import json
from datetime import datetime, timedelta
from pathlib import Path

from pipeline.video.config import CameraRole, EntryLine, Point, PolygonZone, VideoProcessingConfig
from pipeline.video.events import VideoEventGenerator
from pipeline.video.process_videos import write_jsonl_event
from pipeline.video.tracking import TrackSnapshot


BASE_TIME = datetime(2026, 6, 1, 10, 0, 0)


def snapshot(
    *,
    track_id: str = "1",
    camera_id: str = "CAM_TEST",
    timestamp: datetime = BASE_TIME,
    normalized_footpoint: tuple[float, float],
) -> TrackSnapshot:
    width = 100
    height = 100
    x = normalized_footpoint[0] * width
    y = normalized_footpoint[1] * height
    return TrackSnapshot(
        track_id=track_id,
        store_id="STTEST",
        camera_id=camera_id,
        frame_index=0,
        timestamp=timestamp,
        bbox_xyxy=(x - 5, y - 20, x + 5, y),
        confidence=0.9,
        footpoint=(x, y),
        frame_size=(width, height),
    )


def square_zone(zone_id: str = "ZONE_1") -> PolygonZone:
    return PolygonZone(
        id=zone_id,
        name="Test Zone",
        type="SHELF",
        is_revenue_zone=True,
        polygon=(
            Point(0.25, 0.25),
            Point(0.75, 0.25),
            Point(0.75, 0.75),
            Point(0.25, 0.75),
        ),
    )


def config(role: CameraRole, **overrides) -> VideoProcessingConfig:
    values = {
        "store_id": "STTEST",
        "camera_id": "CAM_TEST",
        "role": role,
        "video_path": Path("unused.mp4"),
        "start_time": BASE_TIME,
    }
    values.update(overrides)
    return VideoProcessingConfig(**values)


def test_entry_camera_generates_entry_and_exit_on_line_crossing() -> None:
    generator = VideoEventGenerator(
        config(
            CameraRole.ENTRY,
            entry_line=EntryLine(axis="x", position=0.5, inside_greater_than_position=True),
        )
    )

    assert generator.process_snapshot(snapshot(normalized_footpoint=(0.25, 0.5))) == []
    entry_events = generator.process_snapshot(snapshot(timestamp=BASE_TIME + timedelta(seconds=1), normalized_footpoint=(0.65, 0.5)))
    exit_events = generator.process_snapshot(snapshot(timestamp=BASE_TIME + timedelta(seconds=2), normalized_footpoint=(0.35, 0.5)))

    assert entry_events[0]["event_type"] == "ENTRY"
    assert entry_events[0]["visitor_id"] == "CAM_TEST:1"
    assert entry_events[0]["event_id"]
    assert entry_events[0]["confidence"] == 0.9
    assert exit_events[0]["event_type"] == "EXIT"


def test_entry_camera_generates_reentry_after_prior_exit() -> None:
    generator = VideoEventGenerator(
        config(
            CameraRole.ENTRY,
            entry_line=EntryLine(axis="x", position=0.5, inside_greater_than_position=True),
        )
    )

    generator.process_snapshot(snapshot(normalized_footpoint=(0.25, 0.5)))
    generator.process_snapshot(snapshot(timestamp=BASE_TIME + timedelta(seconds=1), normalized_footpoint=(0.65, 0.5)))
    generator.process_snapshot(snapshot(timestamp=BASE_TIME + timedelta(seconds=2), normalized_footpoint=(0.35, 0.5)))
    reentry = generator.process_snapshot(snapshot(timestamp=BASE_TIME + timedelta(seconds=3), normalized_footpoint=(0.65, 0.5)))

    assert reentry[0]["event_type"] == "REENTRY"


def test_zone_camera_generates_zone_entered_and_exited() -> None:
    generator = VideoEventGenerator(config(CameraRole.ZONE, zones=(square_zone(),)))

    assert generator.process_snapshot(snapshot(normalized_footpoint=(0.10, 0.10))) == []
    entered = generator.process_snapshot(snapshot(timestamp=BASE_TIME + timedelta(seconds=1), normalized_footpoint=(0.50, 0.50)))
    exited = generator.process_snapshot(snapshot(timestamp=BASE_TIME + timedelta(seconds=2), normalized_footpoint=(0.90, 0.90)))

    assert entered[0]["event_type"] == "ZONE_ENTER"
    assert entered[0]["visitor_id"] == "CAM_TEST:1"
    assert entered[0]["zone_id"] == "ZONE_1"
    assert exited[0]["event_type"] == "ZONE_EXIT"


def _billing_config() -> VideoProcessingConfig:
    return config(
        CameraRole.BILLING,
        zones=(square_zone("QUEUE_1"),),
        queue_zone_id="QUEUE_1",
        queue_completion_seconds=30,
        queue_abandonment_seconds=5,
    )


def test_billing_camera_emits_join_event_immediately() -> None:
    """F-01: JOIN must fire the moment a track enters the queue polygon, not
    retroactively once it leaves."""
    generator = VideoEventGenerator(_billing_config())

    joined = generator.process_snapshot(snapshot(normalized_footpoint=(0.50, 0.50)))

    assert len(joined) == 1
    assert joined[0]["event_type"] == "BILLING_QUEUE_JOIN"
    assert joined[0]["metadata"]["queue_event_id"]
    assert joined[0]["metadata"]["queue_join_ts"] is not None


def test_billing_camera_generates_queue_completed_after_threshold() -> None:
    generator = VideoEventGenerator(_billing_config())

    joined = generator.process_snapshot(snapshot(normalized_footpoint=(0.50, 0.50)))
    completed = generator.process_snapshot(
        snapshot(timestamp=BASE_TIME + timedelta(seconds=35), normalized_footpoint=(0.90, 0.90))
    )

    assert joined[0]["event_type"] == "BILLING_QUEUE_JOIN"
    assert completed[0]["event_type"] == "BILLING_QUEUE_COMPLETE"
    assert completed[0]["metadata"]["abandoned"] is False
    assert completed[0]["metadata"]["queue_served_ts"] is None
    assert completed[0]["metadata"]["wait_seconds"] == 35
    assert completed[0]["metadata"]["queue_event_id"] == joined[0]["metadata"]["queue_event_id"]


def test_billing_camera_generates_queue_abandoned_for_short_queue_exit() -> None:
    generator = VideoEventGenerator(_billing_config())

    joined = generator.process_snapshot(snapshot(normalized_footpoint=(0.50, 0.50)))
    abandoned = generator.process_snapshot(
        snapshot(timestamp=BASE_TIME + timedelta(seconds=12), normalized_footpoint=(0.90, 0.90))
    )

    assert joined[0]["event_type"] == "BILLING_QUEUE_JOIN"
    assert abandoned[0]["event_type"] == "BILLING_QUEUE_ABANDON"
    assert abandoned[0]["metadata"]["abandoned"] is True
    assert abandoned[0]["metadata"]["queue_served_ts"] is None
    assert abandoned[0]["metadata"]["wait_seconds"] == 12
    assert abandoned[0]["metadata"]["queue_event_id"] == joined[0]["metadata"]["queue_event_id"]


def test_billing_camera_short_boundary_dwell_emits_join_but_no_terminal_event() -> None:
    """Dwell below queue_abandonment_seconds is boundary noise: per the existing
    design, no terminal event is produced for it. JOIN still fires at entry --
    the pipeline cannot know in advance how long a visit will last."""
    generator = VideoEventGenerator(_billing_config())

    joined = generator.process_snapshot(snapshot(normalized_footpoint=(0.50, 0.50)))
    exited = generator.process_snapshot(
        snapshot(timestamp=BASE_TIME + timedelta(seconds=2), normalized_footpoint=(0.90, 0.90))
    )

    assert len(joined) == 1
    assert joined[0]["event_type"] == "BILLING_QUEUE_JOIN"
    assert exited == []


def test_completed_queue_is_never_labeled_as_join() -> None:
    """Regression guard for the original defect: a completed queue visit's
    terminal event must never come back labeled BILLING_QUEUE_JOIN."""
    generator = VideoEventGenerator(_billing_config())

    generator.process_snapshot(snapshot(normalized_footpoint=(0.50, 0.50)))
    terminal_events = generator.process_snapshot(
        snapshot(timestamp=BASE_TIME + timedelta(seconds=40), normalized_footpoint=(0.90, 0.90))
    )

    assert len(terminal_events) == 1
    assert terminal_events[0]["event_type"] != "BILLING_QUEUE_JOIN"
    assert terminal_events[0]["event_type"] == "BILLING_QUEUE_COMPLETE"


def test_billing_camera_full_visit_produces_exactly_join_and_complete() -> None:
    """Integration-style: JOIN, then an exit after the completion threshold,
    produces exactly [JOIN, COMPLETE] across the interaction -- nothing more,
    nothing mislabeled."""
    generator = VideoEventGenerator(_billing_config())

    all_events: list[dict] = []
    all_events += generator.process_snapshot(snapshot(normalized_footpoint=(0.50, 0.50)))
    all_events += generator.process_snapshot(
        snapshot(timestamp=BASE_TIME + timedelta(seconds=10), normalized_footpoint=(0.55, 0.55))
    )
    all_events += generator.process_snapshot(
        snapshot(timestamp=BASE_TIME + timedelta(seconds=35), normalized_footpoint=(0.90, 0.90))
    )

    assert [event["event_type"] for event in all_events] == ["BILLING_QUEUE_JOIN", "BILLING_QUEUE_COMPLETE"]


def test_billing_camera_full_visit_produces_exactly_join_and_abandon() -> None:
    """Integration-style: JOIN, then an exit after the abandonment threshold but
    before completion, produces exactly [JOIN, ABANDON]."""
    generator = VideoEventGenerator(_billing_config())

    all_events: list[dict] = []
    all_events += generator.process_snapshot(snapshot(normalized_footpoint=(0.50, 0.50)))
    all_events += generator.process_snapshot(
        snapshot(timestamp=BASE_TIME + timedelta(seconds=12), normalized_footpoint=(0.90, 0.90))
    )

    assert [event["event_type"] for event in all_events] == ["BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"]


def test_jsonl_writer_keeps_one_parseable_event_per_line() -> None:
    output = io.StringIO()
    events = [
        {"event_type": "entry", "id_token": "CAM_TEST:1"},
        {"event_type": "zone_entered", "track_id": "CAM_TEST:1", "zone_id": "ZONE_1"},
        {"event_type": "queue_abandoned", "track_id": "CAM_TEST:2", "abandoned": True},
    ]

    for event in events:
        write_jsonl_event(output, event)

    contents = output.getvalue()
    lines = contents.splitlines()

    assert "}{" not in contents
    assert contents.count("\n") == len(events)
    assert len(lines) == len(events)
    assert [json.loads(line) for line in lines] == events
