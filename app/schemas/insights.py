from __future__ import annotations

from pydantic import BaseModel, Field


class StoreInsight(BaseModel):
    category: str
    severity: str
    title: str
    explanation: str
    recommendation: str


class StoreInsightsResponse(BaseModel):
    store_id: str
    insights: list[StoreInsight] = Field(default_factory=list)
