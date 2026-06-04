from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

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
    ) -> Iterator[TrackSnapshot]:
        metadata = read_video_metadata(config.video_path)
        stride = _frame_stride(metadata.fps, config.sample_fps)
        results = self.model.track(
            source=str(config.video_path),
            stream=True,
            persist=True,
            tracker="bytetrack.yaml",
            classes=[0],
            conf=config.confidence_threshold,
            vid_stride=stride,
            verbose=False,
        )

        processed_frames = 0
        for result_index, result in enumerate(results):
            frame_index = result_index * stride
            if max_frames is not None and frame_index >= max_frames:
                break

            boxes = getattr(result, "boxes", None)
            if boxes is None or boxes.id is None:
                continue

            frame_time = config.start_time + timedelta(seconds=frame_index / metadata.fps) if metadata.fps else config.start_time
            frame_size = _result_frame_size(result, metadata)

            for box in boxes:
                if box.id is None:
                    continue

                track_id = str(int(box.id[0]))
                xyxy = tuple(float(value) for value in box.xyxy[0].tolist())
                confidence = float(box.conf[0]) if box.conf is not None else 0.0
                footpoint = ((xyxy[0] + xyxy[2]) / 2.0, xyxy[3])
                yield TrackSnapshot(
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

            processed_frames += 1
            if max_frames is not None and processed_frames >= max_frames:
                break


def _frame_stride(source_fps: float, sample_fps: float) -> int:
    if source_fps <= 0 or sample_fps <= 0:
        return 1
    return max(1, round(source_fps / sample_fps))


def _result_frame_size(result, metadata: VideoMetadata) -> tuple[int, int]:
    orig_shape = getattr(result, "orig_shape", None)
    if orig_shape and len(orig_shape) >= 2:
        return (int(orig_shape[1]), int(orig_shape[0]))
    return (metadata.width, metadata.height)

