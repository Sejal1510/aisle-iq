from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.schemas.live_analytics import PeakHourBucket, PeakHoursResponse
from app.services.time_series_service import TimeSeriesService


class PeakHourService:
    """Ranks hourly (or arbitrary-granularity) footfall within a caller-supplied
    period. Built entirely on top of TimeSeriesService.hourly_footfall rather
    than re-querying Event directly, so bucketing/staff-exclusion stays in one
    place. On-read aggregation only, no materialized/cached rollups."""

    def __init__(self, db: Session):
        self.time_series = TimeSeriesService(db)

    def peak_hours(
        self, store_id: str, start: datetime, end: datetime, bucket_minutes: int = 60
    ) -> PeakHoursResponse:
        footfall = self.time_series.hourly_footfall(store_id, start, end, bucket_minutes=bucket_minutes)

        ranked = sorted(footfall.buckets, key=lambda bucket: (-bucket.entries, bucket.bucket_start))
        ranked_hours = [
            PeakHourBucket(bucket_start=bucket.bucket_start, entries=bucket.entries, rank=rank)
            for rank, bucket in enumerate(ranked, start=1)
        ]
        peak_hour = ranked_hours[0] if ranked_hours and ranked_hours[0].entries > 0 else None

        return PeakHoursResponse(
            store_id=store_id,
            start=footfall.start,
            end=footfall.end,
            peak_hour=peak_hour,
            ranked_hours=ranked_hours,
        )
