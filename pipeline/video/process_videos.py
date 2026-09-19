from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, TextIO

import structlog

from pipeline.video.processing import process_camera
from pipeline.video.tracking import UltralyticsByteTracker, read_video_metadata

logger = structlog.get_logger(__name__)


def write_jsonl_event(output_file: TextIO, event: dict[str, Any]) -> None:
    output_file.write(json.dumps(event, separators=(",", ":")))
    output_file.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate ingestion-compatible JSONL events from CCTV videos.")
    parser.add_argument("--output", default="data/generated_cctv_events.jsonl", help="Path to write generated JSONL.")
    parser.add_argument("--model", default="yolov8n.pt", help="Ultralytics YOLOv8 model name/path.")
    parser.add_argument("--sample-fps", type=float, default=None, help="Override configured frame sampling FPS.")
    parser.add_argument("--max-frames", type=int, default=None, help="Optional cap for smoke tests.")
    parser.add_argument("--metadata-only", action="store_true", help="Inspect configured videos without running inference.")
    parser.add_argument(
        "--store-id", default=None, help="Only process cameras configured for this store. Default: all stores."
    )
    args = parser.parse_args()

    # P7: camera/zone/video-path configuration is loaded from the database
    # (populated via the onboarding API/UI, or -- for ST1001/ST1002 -- via
    # pipeline.migrate_legacy_store_config) instead of a hardcoded Python
    # function. See pipeline/video/config.py's load_video_configs_from_db.
    from app.db.session import SessionLocal, init_db
    from pipeline.video.config import load_video_configs_from_db

    init_db()
    db = SessionLocal()
    try:
        configs = load_video_configs_from_db(db, store_id=args.store_id)
    finally:
        db.close()

    if not configs:
        logger.warning(
            "no_video_configs_found",
            store_id=args.store_id,
            hint="Onboard at least one store/camera with a video_path, start_time, and role via the onboarding API before running this command.",
        )
        return

    if args.sample_fps is not None:
        configs = [
            config.__class__(
                **{
                    **config.__dict__,
                    "sample_fps": args.sample_fps,
                }
            )
            for config in configs
        ]

    if args.metadata_only:
        for config in configs:
            metadata = read_video_metadata(config.video_path)
            logger.info(
                "video_metadata",
                store_id=config.store_id,
                camera_id=config.camera_id,
                role=config.role.value,
                video_path=str(config.video_path),
                fps=metadata.fps,
                frame_count=metadata.frame_count,
                width=metadata.width,
                height=metadata.height,
                duration_seconds=round(metadata.duration_seconds, 2),
            )
        return

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tracker = UltralyticsByteTracker(model_name=args.model)
    total_events = 0

    with output_path.open("w", encoding="utf-8") as output_file:
        for config in configs:
            camera_events = 0
            logger.info(
                "processing_video",
                store_id=config.store_id,
                camera_id=config.camera_id,
                role=config.role.value,
                video_path=str(config.video_path),
            )

            for event in process_camera(config, tracker, max_frames=args.max_frames):
                write_jsonl_event(output_file, event)
                camera_events += 1

            total_events += camera_events
            logger.info("video_events_generated", camera_id=config.camera_id, events=camera_events)

    logger.info("cctv_event_generation_complete", output=str(output_path), events=total_events)


if __name__ == "__main__":
    main()
