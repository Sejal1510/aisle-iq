from __future__ import annotations

import argparse
import signal

import structlog

logger = structlog.get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the P9 video-processing worker.")
    parser.add_argument("--model", default="yolov8n.pt", help="Ultralytics YOLOv8 model name/path.")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Override the configured polling interval (seconds) between empty claim attempts.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll iteration (claim/process at most one job) and exit, instead of looping forever.",
    )
    args = parser.parse_args()

    # Deferred, same as pipeline/video/process_videos.py's main() -- keeps
    # this module importable (e.g. by a future test) without pulling in the
    # whole app/db stack or loading a YOLO model at import time.
    from sqlalchemy.orm import sessionmaker

    from app.db.session import init_db
    from app.db.worker_session import create_worker_engine
    from app.worker.video_processing_worker import VideoProcessingWorker
    from pipeline.video.tracking import UltralyticsByteTracker

    # Schema readiness is a property of the database itself, not of which
    # Engine object connects to it -- reusing the API's own init_db() here
    # (development: create_all bootstrap; every other environment: verify
    # the Alembic head) means the worker fails the same way the API would
    # against an unmigrated database, rather than discovering a missing
    # table later, mid-job, as an opaque SQL error.
    init_db()

    # Built once, before the poll loop starts and before any job exists to
    # process: if the model/ultralytics can't load, fail startup clearly
    # rather than accepting jobs a broken worker could never run.
    tracker = UltralyticsByteTracker(model_name=args.model)

    from app.core.config import get_settings

    engine = create_worker_engine(get_settings().database_url)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = session_factory()

    worker = VideoProcessingWorker(db, tracker, poll_interval_seconds=args.poll_interval)

    def _handle_stop_signal(signum: int, _frame: object) -> None:
        logger.info("video_worker_stop_requested", signal=signum)
        worker.stop()

    # A simple flag-based stop (VideoProcessingWorker.stop), not elaborate
    # signal-management infrastructure: SIGINT/SIGTERM just request that
    # run_forever() exit after its current iteration.
    signal.signal(signal.SIGINT, _handle_stop_signal)
    signal.signal(signal.SIGTERM, _handle_stop_signal)

    logger.info(
        "video_worker_starting",
        model=args.model,
        poll_interval_seconds=worker.poll_interval_seconds,
        mode="once" if args.once else "forever",
    )
    try:
        if args.once:
            worker.run_once()
        else:
            worker.run_forever()
    finally:
        db.close()
        logger.info("video_worker_stopped")


if __name__ == "__main__":
    main()
