from __future__ import annotations

from pydantic import BaseModel, Field


class PathMetric(BaseModel):
    path: list[str]
    visitor_count: int
    purchase_count: int
    conversion_rate: float
    abandonment_count: int
    abandonment_rate: float


class StorePathAnalyticsResponse(BaseModel):
    store_id: str
    total_journeys: int
    most_common_paths: list[PathMetric] = Field(default_factory=list)
    top_purchase_journeys: list[PathMetric] = Field(default_factory=list)
    path_drop_offs: list[PathMetric] = Field(default_factory=list)
