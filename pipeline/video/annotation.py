"""Annotated demo-video rendering for the P9 video-processing pipeline.

This module draws what the *existing* pipeline already computed -- YOLOv8
person boxes, ByteTrack track ids and detection confidence -- onto the
frames the tracker already decoded. It runs no model of its own and never
changes a detection: ``UltralyticsByteTracker.track_video`` hands each
sampled frame plus that frame's ``TrackSnapshot`` list to
``AnnotatedVideoRenderer.on_frame``, and the renderer only draws and writes.

Output is a separate file from the uploaded original (see
``annotated_output_paths``). It plays at the source frame rate: analysed
frames show the real detections, and frames in between show those same
boxes moved between their analysed positions (see AnnotatedVideoRenderer).

Rendering is strictly best-effort: if OpenCV can't open any browser-playable
encoder, or a write fails mid-video, the renderer disables itself and
records why in ``RunStats.annotation_error``. Detection, tracking and event
generation carry on unaffected -- an annotated video is a visualization, not
a dependency of the analytics.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pipeline.video.tracking import TrackSnapshot

logger = structlog.get_logger(__name__)

# Tried in order. H.264 MP4 plays natively in every mainstream browser; VP8
# WebM is the fallback for OpenCV builds without an H.264 encoder.
_CODEC_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("avc1", ".mp4", "video/mp4"),
    ("VP80", ".webm", "video/webm"),
)

# One readable colour per track id (BGR), cycled -- the same id always gets
# the same colour, so a person is easy to follow across frames.
_TRACK_COLOURS_BGR: tuple[tuple[int, int, int], ...] = (
    (64, 160, 46),
    (215, 120, 30),
    (40, 90, 220),
    (160, 60, 170),
    (30, 170, 200),
    (120, 120, 30),
)


@dataclass
class RunStats:
    """What the tracker actually observed during one processing run. Every
    number here is counted from real tracker output, never estimated."""

    frames_sampled: int = 0
    person_detections: int = 0
    max_people_in_frame: int = 0
    track_first_seen: dict[str, datetime] = field(default_factory=dict)
    track_last_seen: dict[str, datetime] = field(default_factory=dict)
    # Analysed-frame sightings per track, and how many of those had the
    # person's footpoint inside an operator-configured staff-only area.
    track_frames: dict[str, int] = field(default_factory=dict)
    track_staff_area_frames: dict[str, int] = field(default_factory=dict)
    # Tracks the end-of-video finalizer classified as staff (filled in by it).
    staff_tracks: list[str] = field(default_factory=list)
    video_duration_seconds: float | None = None
    sample_fps: float | None = None
    annotated_file: str | None = None
    annotated_content_type: str | None = None
    annotation_error: str | None = None

    def observe(self, snapshots: list[TrackSnapshot]) -> None:
        self.frames_sampled += 1
        self.person_detections += len(snapshots)
        self.max_people_in_frame = max(self.max_people_in_frame, len(snapshots))
        for snapshot in snapshots:
            identity = f"{snapshot.camera_id}:{snapshot.track_id}"
            self.track_first_seen.setdefault(identity, snapshot.timestamp)
            self.track_last_seen[identity] = snapshot.timestamp
            self.track_frames[identity] = self.track_frames.get(identity, 0) + 1

    def record_staff_area_sighting(self, snapshot: TrackSnapshot) -> None:
        identity = f"{snapshot.camera_id}:{snapshot.track_id}"
        self.track_staff_area_frames[identity] = self.track_staff_area_frames.get(identity, 0) + 1

    @property
    def unique_tracks(self) -> int:
        return len(self.track_last_seen)

    def to_summary(self) -> dict[str, Any]:
        return {
            "frames_sampled": self.frames_sampled,
            "person_detections": self.person_detections,
            "unique_tracks": self.unique_tracks,
            "max_people_in_frame": self.max_people_in_frame,
            "video_duration_seconds": self.video_duration_seconds,
            "sample_fps": self.sample_fps,
            "annotated_file": self.annotated_file,
            "annotated_content_type": self.annotated_content_type,
            "annotation_error": self.annotation_error,
            "staff_tracks": self.staff_tracks,
        }


@dataclass(frozen=True)
class AnnotationRequest:
    """Asks ``process_camera`` to also render an annotated copy of the video
    and collect ``RunStats``. ``output_stem`` is a path without extension --
    the renderer appends whichever container its codec needs."""

    output_stem: Path
    stats: RunStats = field(default_factory=RunStats, compare=False)


def annotated_output_paths(videos_dir: Path, store_id: str, job_id: str) -> tuple[Path, Path]:
    """(output_stem, summary_json_path) for one job's annotated output:
    ``<videos_dir>/<store_id>/annotated/<job_id>`` -- inside the existing
    video storage root, in its own subfolder so it can never be confused
    with (or overwrite) an uploaded original."""
    stem = videos_dir / store_id / "annotated" / job_id
    return stem, stem.with_suffix(".json")


def write_run_summary(path: Path, stats: RunStats) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats.to_summary(), indent=2), encoding="utf-8")


def read_run_summary(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class AnnotatedVideoRenderer:
    """Writes the annotated video.

    Full-rate mode (used whenever the original file can be decoded a second
    time): every source frame is written at the source frame rate, so
    playback is smooth. Detection still runs only on the sampled frames --
    frames in between show those same detections, each box moved linearly
    between its positions on the analysed frames either side (see
    ``interpolate_snapshots``). Nothing new is detected or tracked, and the
    analytics never see these in-between boxes.

    Sampled mode (fallback): only the analysed frames, at the sampling rate.
    """

    def __init__(
        self,
        request: AnnotationRequest,
        *,
        fps: float,
        source_path: Path | None = None,
        source_fps: float | None = None,
    ):
        self.request = request
        self.stats = request.stats
        self.fps = max(fps, 1.0)
        self._writer = None
        self._disabled = False
        self._source = None
        self._source_index = -1
        self._previous: tuple[int, list[TrackSnapshot], datetime] | None = None
        if source_path is not None and source_fps and source_fps > self.fps:
            self._source = _open_source(source_path)
            if self._source is not None:
                self.fps = source_fps

    def on_frame(self, frame: Any, snapshots: list[TrackSnapshot], frame_time: datetime, frame_index: int) -> None:
        self.stats.observe(snapshots)
        if self._disabled:
            return
        try:
            if self._source is not None:
                self._render_full_rate(snapshots, frame_time, frame_index)
                return
            if frame is None:
                return
            if self._writer is None:
                self._open(frame)
                if self._writer is None:
                    return
            self._writer.write(self._draw(frame.copy(), snapshots, frame_time))
        except Exception as exc:  # noqa: BLE001 - visualization must never break analytics
            self._fail(f"Annotated video rendering failed: {exc}")

    def _render_full_rate(self, snapshots: list[TrackSnapshot], frame_time: datetime, frame_index: int) -> None:
        """Write every source frame up to and including ``frame_index``."""
        previous = self._previous
        while self._source_index < frame_index:
            ok, raw = self._source.read()
            if not ok:
                raise RuntimeError("the source video ended before an analysed frame")
            self._source_index += 1
            if self._writer is None:
                self._open(raw)
                if self._writer is None:
                    return
            if self._source_index == frame_index:
                boxes, when = snapshots, frame_time
            elif previous is None:
                boxes, when = [], frame_time
            else:
                previous_index, previous_snapshots, previous_time = previous
                fraction = (self._source_index - previous_index) / (frame_index - previous_index)
                boxes = interpolate_snapshots(previous_snapshots, snapshots, fraction)
                when = previous_time + (frame_time - previous_time) * fraction
            self._writer.write(self._draw(raw, boxes, when))
        self._previous = (frame_index, snapshots, frame_time)

    def close(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        if self._source is not None:
            self._source.release()
            self._source = None

    def _open(self, frame: Any) -> None:
        import cv2

        height, width = frame.shape[:2]
        self.request.output_stem.parent.mkdir(parents=True, exist_ok=True)
        for fourcc, suffix, content_type in _CODEC_CANDIDATES:
            path = self.request.output_stem.with_suffix(suffix)
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*fourcc), self.fps, (width, height))
            if writer.isOpened():
                self._writer = writer
                self.stats.annotated_file = path.name
                self.stats.annotated_content_type = content_type
                return
            writer.release()
            path.unlink(missing_ok=True)
        self._fail("No browser-playable video encoder (H.264 or VP8) is available to OpenCV.")

    def _fail(self, message: str) -> None:
        logger.warning("annotated_video_disabled", reason=message)
        self._disabled = True
        self.stats.annotation_error = message
        self.close()
        if self.stats.annotated_file:
            (self.request.output_stem.parent / self.stats.annotated_file).unlink(missing_ok=True)
        self.stats.annotated_file = None
        self.stats.annotated_content_type = None

    def _draw(self, frame: Any, snapshots: list[TrackSnapshot], frame_time: datetime) -> Any:
        import cv2

        height = frame.shape[0]
        scale = max(0.5, height / 1080)
        thickness = max(2, round(2 * scale))
        font = cv2.FONT_HERSHEY_SIMPLEX
        for snapshot in snapshots:
            x1, y1, x2, y2 = (int(round(value)) for value in snapshot.bbox_xyxy)
            colour = _colour_for(snapshot.track_id)
            cv2.rectangle(frame, (x1, y1), (x2, y2), colour, thickness)
            label = f"ID {snapshot.track_id}  {snapshot.confidence:.2f}"
            font_scale = 0.55 * scale
            (text_w, text_h), baseline = cv2.getTextSize(label, font, font_scale, 1)
            label_top = max(0, y1 - text_h - baseline - 6)
            cv2.rectangle(frame, (x1, label_top), (x1 + text_w + 8, label_top + text_h + baseline + 6), colour, -1)
            cv2.putText(
                frame, label, (x1 + 4, label_top + text_h + 2), font, font_scale, (255, 255, 255), 1, cv2.LINE_AA
            )

        banner = f"{frame_time.strftime('%H:%M:%S')}  |  people in frame: {len(snapshots)}"
        cv2.putText(frame, banner, (12, int(32 * scale)), font, 0.7 * scale, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(frame, banner, (12, int(32 * scale)), font, 0.7 * scale, (255, 255, 255), 1, cv2.LINE_AA)
        return frame


def interpolate_snapshots(
    before: list[TrackSnapshot], after: list[TrackSnapshot], fraction: float
) -> list[TrackSnapshot]:
    """Boxes to draw on a frame ``fraction`` (0-1) of the way between two
    analysed frames. A track present on both is moved linearly between its
    two real boxes; a track on only one of them is shown on the half of the
    gap nearest that frame. Display only -- never fed back into analytics."""
    after_by_id = {snapshot.track_id: snapshot for snapshot in after}
    before_ids = {snapshot.track_id for snapshot in before}
    boxes: list[TrackSnapshot] = []
    for snapshot in before:
        target = after_by_id.get(snapshot.track_id)
        if target is None:
            if fraction < 0.5:
                boxes.append(snapshot)
            continue
        bbox = tuple(a + (b - a) * fraction for a, b in zip(snapshot.bbox_xyxy, target.bbox_xyxy, strict=True))
        boxes.append(replace(snapshot if fraction < 0.5 else target, bbox_xyxy=bbox))
    if fraction >= 0.5:
        boxes.extend(snapshot for snapshot in after if snapshot.track_id not in before_ids)
    return boxes


def _open_source(path: Path) -> Any:
    try:
        import cv2

        capture = cv2.VideoCapture(str(path))
    except Exception:  # noqa: BLE001 - fall back to sampled-rate rendering
        return None
    if not capture.isOpened():
        capture.release()
        return None
    return capture


def _colour_for(track_id: str) -> tuple[int, int, int]:
    try:
        index = int(track_id)
    except ValueError:
        index = sum(ord(char) for char in track_id)
    return _TRACK_COLOURS_BGR[index % len(_TRACK_COLOURS_BGR)]
