from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pipeline.video.config import VideoProcessingConfig
from pipeline.video.events import VideoEventGenerator
from pipeline.video.tracking import UltralyticsByteTracker


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
    for snapshot in tracker.track_video(config, max_frames=max_frames):
        yield from generator.process_snapshot(snapshot)
    yield from generator.finalize()
