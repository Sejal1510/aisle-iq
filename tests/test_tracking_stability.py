"""Within-camera tracking stability: ByteTrack settings for 2 fps CCTV.

These drive Ultralytics' real BYTETracker with synthetic detections, so they
test the actual association behavior the settings produce -- not a mock of
it. Cross-camera identity is out of scope: each video gets its own tracker.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
byte_tracker = pytest.importorskip("ultralytics.trackers.byte_tracker")

from pipeline.video.config import CameraRole, VideoProcessingConfig  # noqa: E402
from pipeline.video.tracking import (  # noqa: E402
    ByteTrackSettings,
    UltralyticsByteTracker,
)

ULTRALYTICS_DEFAULTS = {
    "track_high_thresh": 0.25, "track_low_thresh": 0.1, "new_track_thresh": 0.25, "track_buffer": 30,
    "match_thresh": 0.8, "fuse_score": True,
}


class _Detections:
    """Minimal numpy-backed stand-in for Ultralytics' Boxes."""

    def __init__(self, boxes: list[tuple[float, float, float, float, float]]):
        data = np.array(boxes, dtype=np.float32).reshape(-1, 5)
        self.xywh = data[:, :4]
        self.conf = data[:, 4]
        self.cls = np.zeros(len(data), dtype=np.float32)

    def __len__(self) -> int:
        return len(self.conf)

    def __getitem__(self, index):
        picked = _Detections([])
        picked.xywh, picked.conf, picked.cls = self.xywh[index], self.conf[index], self.cls[index]
        return picked


def _tracker(settings: dict) -> byte_tracker.BYTETracker:
    return byte_tracker.BYTETracker(SimpleNamespace(**settings))


def _ours() -> dict:
    s = ByteTrackSettings.for_camera(0.35)
    return {
        "track_high_thresh": s.track_high_thresh, "track_low_thresh": s.track_low_thresh,
        "new_track_thresh": s.new_track_thresh, "track_buffer": s.track_buffer, "match_thresh": s.match_thresh,
        "fuse_score": s.fuse_score,
    }


def _step(tracker, boxes) -> dict[int, tuple[float, float]]:
    """Feed one frame; return {track_id: (centre_x, centre_y)}."""
    out = tracker.update(_Detections(boxes))
    return {int(row[4]): ((row[0] + row[2]) / 2, (row[1] + row[3]) / 2) for row in out}


def test_settings_follow_the_camera_confidence_threshold() -> None:
    settings = ByteTrackSettings.for_camera(0.4)

    assert settings.track_high_thresh == settings.new_track_thresh == 0.4
    assert settings.track_low_thresh == 0.1
    assert settings.track_buffer == 30
    assert "match_thresh: 0.9" in settings.to_yaml()


def test_a_person_moving_far_between_2fps_samples_stays_tracked() -> None:
    """A shopper walking past at 2 fps: 45 px per analysed frame on a 60 px
    box, so each new detection overlaps the previous box by only ~0.14 IoU.
    With Ultralytics' default gate (score-fused IoU >= 0.2) the person is
    reported once and then lost -- every re-detection fails association.
    The tuned gate keeps reporting the same person under one id."""
    path = [(100 + 45 * i, 200, 60, 160, 0.9) for i in range(8)]

    def run(settings):
        tracker = _tracker(settings)
        return [_step(tracker, [box]) for box in path]

    default_frames = run(ULTRALYTICS_DEFAULTS)
    tuned_frames = run(_ours())

    assert sum(1 for frame in default_frames if frame) == 1
    assert all(tuned_frames)
    assert len({track_id for frame in tuned_frames for track_id in frame}) == 1


def test_boxes_that_never_overlap_are_not_associated() -> None:
    """The looser gate still needs real overlap: a detection that doesn't
    overlap the predicted box (a different person, or a jump the tracker
    can't explain) is never attached to an existing id."""
    tracker = _tracker(_ours())
    _step(tracker, [(100, 200, 60, 160, 0.9)])

    assert _step(tracker, [(400, 200, 60, 160, 0.9)]) == {}


def test_a_confidence_dip_keeps_the_existing_track_alive() -> None:
    tracker = _tracker(_ours())
    confidences = [0.9, 0.9, 0.2, 0.2, 0.9]
    frames = [_step(tracker, [(200 + 5 * i, 200, 60, 160, conf)]) for i, conf in enumerate(confidences)]

    ids = {track_id for frame in frames for track_id in frame}
    assert len(ids) == 1
    assert all(frames), "the person is still reported during the low-confidence frames"


def test_a_low_confidence_detection_alone_never_creates_a_person() -> None:
    tracker = _tracker(_ours())

    for _ in range(4):
        assert _step(tracker, [(300, 300, 60, 160, 0.2)]) == {}


def test_a_short_occlusion_resumes_the_same_id() -> None:
    tracker = _tracker(_ours())
    before = set()
    for i in range(4):
        before |= set(_step(tracker, [(200 + 10 * i, 200, 60, 160, 0.9)]))
    for _ in range(3):  # 1.5 s hidden at 2 fps
        _step(tracker, [])
    after = set(_step(tracker, [(270, 200, 60, 160, 0.9)]))

    assert after == before


def test_a_long_disappearance_is_allowed_to_become_a_new_id() -> None:
    tracker = _tracker(_ours())
    first = set()
    for _ in range(3):
        first |= set(_step(tracker, [(200, 200, 60, 160, 0.9)]))
    for _ in range(ByteTrackSettings().track_buffer + 2):
        _step(tracker, [])
    _step(tracker, [(200, 200, 60, 160, 0.9)])
    later = set(_step(tracker, [(200, 200, 60, 160, 0.9)]))

    assert later and later.isdisjoint(first)


def test_two_people_standing_close_keep_separate_ids() -> None:
    tracker = _tracker(_ours())
    seen = []
    for i in range(10):
        jitter = (-1) ** i * 2
        seen.append(_step(tracker, [(200 + jitter, 200, 60, 160, 0.9), (245 - jitter, 205, 60, 160, 0.85)]))

    assert all(len(frame) == 2 for frame in seen)
    assert len({track_id for frame in seen for track_id in frame}) == 2
    left_id = min(seen[0], key=lambda track_id: seen[0][track_id][0])
    assert all(frame[left_id][0] < 225 for frame in seen)  # the left person's id never moves to the right person


def test_two_people_crossing_keep_their_own_ids() -> None:
    tracker = _tracker(_ours())
    frames = []
    for i in range(9):
        a = (100 + 30 * i, 200, 60, 160, 0.9)  # walking right
        b = (340 - 30 * i, 210, 60, 160, 0.9)  # walking left
        frames.append(_step(tracker, [a, b]))

    first, last = frames[0], frames[-1]
    walker_right = min(first, key=lambda track_id: first[track_id][0])
    walker_left = max(first, key=lambda track_id: first[track_id][0])
    assert walker_right != walker_left
    assert last[walker_right][0] > last[walker_left][0]  # each id followed its own person through the crossing
    assert len({track_id for frame in frames for track_id in frame}) == 2


def test_each_video_gets_a_fresh_tracker_with_the_tuned_settings(tmp_path: Path) -> None:
    cv2 = pytest.importorskip("cv2")
    video = tmp_path / "v.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (64, 64))
    for _ in range(4):
        writer.write(np.zeros((64, 64, 3), np.uint8))
    writer.release()

    calls = []

    class _FakeModel:
        def track(self, **kwargs):
            calls.append({**kwargs, "tracker_yaml": Path(kwargs["tracker"]).read_text(encoding="utf-8")})
            return iter([])

    tracker = UltralyticsByteTracker.__new__(UltralyticsByteTracker)
    tracker.model = _FakeModel()
    config = VideoProcessingConfig(
        store_id="ST", camera_id="CAM", role=CameraRole.ENTRY, video_path=video, start_time=datetime(2026, 6, 1)
    )

    list(tracker.track_video(config))
    list(tracker.track_video(config))

    assert len(calls) == 2
    assert all(call["persist"] is False for call in calls)  # no lost tracks carried into the next video
    assert all(call["conf"] == 0.1 for call in calls)
    assert "new_track_thresh: 0.35" in calls[0]["tracker_yaml"]
    assert "match_thresh: 0.9" in calls[0]["tracker_yaml"]
