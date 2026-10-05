from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline.video.config import VideoProcessingConfig


@dataclass(frozen=True)
class VideoMetadata:
    fps: float
    frame_count: int
    width: int
    height: int
    duration_seconds: float


@dataclass(frozen=True)
class TrackSnapshot:
    track_id: str
    store_id: str
    camera_id: str
    frame_index: int
    timestamp: datetime
    bbox_xyxy: tuple[float, float, float, float]
    confidence: float
    footpoint: tuple[float, float]
    frame_size: tuple[int, int]

    @property
    def normalized_footpoint(self) -> tuple[float, float]:
        width, height = self.frame_size
        if width <= 0 or height <= 0:
            return (0.0, 0.0)
        return (self.footpoint[0] / width, self.footpoint[1] / height)


def read_video_metadata(video_path: Path) -> VideoMetadata:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python is required to inspect video metadata.") from exc

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"Unable to open video: {video_path}")

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    capture.release()

    duration = frame_count / fps if fps > 0 else 0.0
    return VideoMetadata(
        fps=fps,
        frame_count=frame_count,
        width=width,
        height=height,
        duration_seconds=duration,
    )


@dataclass(frozen=True)
class ByteTrackSettings:
    """ByteTrack parameters for sampled CCTV footage (P9 samples ~2 fps).

    Ultralytics' defaults assume a full-rate (~30 fps) stream. At 2 fps a
    walking shopper moves roughly 0.7 m between analysed frames, so the
    overlap between a track's predicted box and that person's next detection
    is often below the default gate (IoU 0.2, match_thresh 0.8) and the
    tracker starts a new id for the same person. Values below were chosen by
    measuring the Store 2 footage:

    - track_high_thresh / new_track_thresh = the camera's confidence
      threshold (0.35 by default): the same bar a detection always needed to
      count as a person, now applied where ByteTrack decides to trust a
      detection or open a new id.
    - track_low_thresh 0.1: detections between 0.1 and the high threshold
      can only *extend* an existing track (ByteTrack's second stage, which
      the old conf=0.35 detector filter switched off entirely); they can
      never create a new person.
    - match_thresh 0.9: first-stage association accepts IoU >= 0.1 instead
      of >= 0.2 to tolerate 2 fps motion. Still requires real box overlap
      with the Kalman-predicted position -- people whose boxes don't overlap
      are never associated.
    - track_buffer 30 (unchanged): counted in *processed* frames, so 15 s at
      2 fps. Measured: lowering it fragmented more; raising it is not needed.
    - fuse_score True (unchanged).
    """

    track_high_thresh: float = 0.35
    track_low_thresh: float = 0.1
    new_track_thresh: float = 0.35
    track_buffer: int = 30
    match_thresh: float = 0.9
    fuse_score: bool = True

    @classmethod
    def for_camera(cls, confidence_threshold: float) -> ByteTrackSettings:
        return cls(
            track_high_thresh=confidence_threshold,
            new_track_thresh=confidence_threshold,
            track_low_thresh=min(cls.track_low_thresh, confidence_threshold),
        )

    def to_yaml(self) -> str:
        values = {
            "tracker_type": "bytetrack",
            "track_high_thresh": self.track_high_thresh,
            "track_low_thresh": self.track_low_thresh,
            "new_track_thresh": self.new_track_thresh,
            "track_buffer": self.track_buffer,
            "match_thresh": self.match_thresh,
            "fuse_score": self.fuse_score,
        }
        return "".join(f"{key}: {value}\n" for key, value in values.items())


class UltralyticsByteTracker:
    """Runs YOLOv8 person detection and ByteTrack tracking via Ultralytics."""

    def __init__(self, model_name: str = "yolov8n.pt"):
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError(
                "Ultralytics is required for YOLOv8 + ByteTrack processing. "
                "Install dependencies from requirements.txt."
            ) from exc

        self.model = YOLO(model_name)

    def track_video(
        self,
        config: VideoProcessingConfig,
        *,
        max_frames: int | None = None,
        frame_callback: FrameCallback | None = None,
    ) -> Iterator[TrackSnapshot]:
        """Yield one ``TrackSnapshot`` per tracked person per sampled frame.

        ``frame_callback``, if given, is called once per sampled frame --
        including frames with nobody in them -- with the decoded frame, that
        frame's snapshots, its timestamp and its index in the source video,
        after the snapshots have been
        yielded. It is a read-only observer (used to render the annotated
        demo video); it cannot alter what this generator yields.
        """
        metadata = read_video_metadata(config.video_path)
        stride = _frame_stride(metadata.fps, config.sample_fps)
        settings = ByteTrackSettings.for_camera(config.confidence_threshold)
        with tempfile.TemporaryDirectory(prefix="aisleiq-bytetrack-") as tracker_dir:
            tracker_yaml = Path(tracker_dir) / "bytetrack.yaml"
            tracker_yaml.write_text(settings.to_yaml(), encoding="utf-8")
            results = self.model.track(
                source=str(config.video_path),
                stream=True,
                # A fresh tracker for every video. persist=True kept the
                # previous video's tracker -- including its *lost* tracks --
                # alive across calls in the long-running worker, so a person
                # last seen at the end of one camera's video could be
                # re-activated by a detection at the start of another
                # camera's video (observed on Store 2: one track id spanned
                # Entrance Camera 1 and Entrance Camera 2).
                persist=False,
                tracker=str(tracker_yaml),
                classes=[0],
                # Detections down to ByteTrack's low threshold are passed
                # to the tracker so its second association stage can keep an
                # existing track alive through a dip in detector confidence.
                # A *new* track (a new person) still needs
                # new_track_thresh, i.e. the camera's confidence threshold.
                conf=settings.track_low_thresh,
                vid_stride=stride,
                verbose=False,
            )
            yield from self._snapshots(results, config, metadata, stride, max_frames, frame_callback)

    def _snapshots(
        self,
        results,
        config: VideoProcessingConfig,
        metadata: VideoMetadata,
        stride: int,
        max_frames: int | None,
        frame_callback: FrameCallback | None,
    ) -> Iterator[TrackSnapshot]:
        processed_frames = 0
        for result_index, result in enumerate(results):
            frame_index = result_index * stride
            if max_frames is not None and frame_index >= max_frames:
                break

            frame_time = config.start_time + timedelta(seconds=frame_index / metadata.fps) if metadata.fps else config.start_time
            boxes = getattr(result, "boxes", None)
            if boxes is None or boxes.id is None:
                if frame_callback is not None:
                    frame_callback(getattr(result, "orig_img", None), [], frame_time, frame_index)
                continue

            frame_size = _result_frame_size(result, metadata)

            frame_snapshots: list[TrackSnapshot] = []
            for box in boxes:
                if box.id is None:
                    continue

                track_id = str(int(box.id[0]))
                xyxy = tuple(float(value) for value in box.xyxy[0].tolist())
                confidence = float(box.conf[0]) if box.conf is not None else 0.0
                footpoint = ((xyxy[0] + xyxy[2]) / 2.0, xyxy[3])
                snapshot = TrackSnapshot(
                    track_id=track_id,
                    store_id=config.store_id,
                    camera_id=config.camera_id,
                    frame_index=frame_index,
                    timestamp=frame_time,
                    bbox_xyxy=xyxy,
                    confidence=confidence,
                    footpoint=footpoint,
                    frame_size=frame_size,
                )
                frame_snapshots.append(snapshot)
                yield snapshot

            if frame_callback is not None:
                frame_callback(getattr(result, "orig_img", None), frame_snapshots, frame_time, frame_index)

            processed_frames += 1
            if max_frames is not None and processed_frames >= max_frames:
                break


# (decoded frame or None, that frame's snapshots, frame timestamp, source frame index)
FrameCallback = Callable[[Any, list["TrackSnapshot"], datetime, int], None]


def _frame_stride(source_fps: float, sample_fps: float) -> int:
    if source_fps <= 0 or sample_fps <= 0:
        return 1
    return max(1, round(source_fps / sample_fps))


def _result_frame_size(result, metadata: VideoMetadata) -> tuple[int, int]:
    orig_shape = getattr(result, "orig_shape", None)
    if orig_shape and len(orig_shape) >= 2:
        return (int(orig_shape[1]), int(orig_shape[0]))
    return (metadata.width, metadata.height)

