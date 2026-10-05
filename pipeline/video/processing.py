from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pipeline.video.annotation import AnnotatedVideoRenderer
from pipeline.video.config import VideoProcessingConfig
from pipeline.video.events import VideoEventGenerator, footpoint_in_zone
from pipeline.video.tracking import UltralyticsByteTracker, read_video_metadata


def process_camera(
    config: VideoProcessingConfig,
    tracker: UltralyticsByteTracker,
    *,
    max_frames: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Run one camera's video through tracking + event generation, yielding
    every generated event dict (the same CanonicalEvent-shaped payloads
    ``VideoEventGenerator`` always produced) in emission order, including
    ``finalize()``'s trailing queue-abandonment events.

    This is the reusable core lifted out of ``process_videos.py``'s CLI loop
    body so both the CLI (writes each dict to a JSONL file) and the P9
    worker (feeds each dict through ``EventIngestionService.process_event``)
    call the exact same tracking/event-generation logic -- there is no
    parallel implementation of either. Pure generator with no I/O side
    effects of its own: what a caller does with each yielded dict is
    entirely up to it.
    """
    generator = VideoEventGenerator(config)
    renderer = _renderer_for(config)
    staff_areas = [zone for zone in config.zones if zone.is_staff_area]
    try:
        track_kwargs = {"frame_callback": renderer.on_frame} if renderer is not None else {}
        for snapshot in tracker.track_video(config, max_frames=max_frames, **track_kwargs):
            if config.annotation is not None and any(footpoint_in_zone(snapshot, zone) for zone in staff_areas):
                config.annotation.stats.record_staff_area_sighting(snapshot)
            yield from generator.process_snapshot(snapshot)
    finally:
        if renderer is not None:
            renderer.close()
    yield from generator.finalize()


def _renderer_for(config: VideoProcessingConfig) -> AnnotatedVideoRenderer | None:
    """An annotated-video renderer when the caller asked for one (the P9
    worker does; the offline JSONL CLI doesn't). Inference stays at the
    sampling rate; the renderer only re-decodes the original for smooth
    playback."""
    if config.annotation is None:
        return None
    stats = config.annotation.stats
    stats.sample_fps = config.sample_fps
    source_fps = None
    try:
        metadata = read_video_metadata(config.video_path)
        stats.video_duration_seconds = metadata.duration_seconds
        source_fps = metadata.fps
    except Exception:  # noqa: BLE001 - metadata is informational only
        stats.video_duration_seconds = None
    return AnnotatedVideoRenderer(
        config.annotation, fps=config.sample_fps, source_path=config.video_path, source_fps=source_fps
    )
