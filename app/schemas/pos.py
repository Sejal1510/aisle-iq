from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class PosRow(BaseModel):
    order_id: str = Field(min_length=1)
    order_date: str = Field(min_length=1)
    order_time: str = Field(min_length=1)
    store_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    brand_name: Optional[str] = None
    total_amount: float
    timestamp: datetime | None = None

    @field_validator("order_id", "order_date", "order_time", "store_id", "product_id", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("brand_name", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("store_id", mode="after")
    @classmethod
    def normalize_store_id(cls, value: str) -> str:
        store_id = value.strip().upper()
        match = re.fullmatch(r"(?:STORE[_\-\s]*)?(\d+)|ST(\d+)", store_id)
        if not match:
            raise ValueError("store_id must look like ST1008 or store_1008")
        numeric_id = match.group(1) or match.group(2)
        return f"ST{numeric_id}"

    @field_validator("total_amount", mode="before")
    @classmethod
    def parse_total_amount(cls, value: object) -> float:
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, AttributeError):
            raise ValueError("total_amount must be numeric") from None

        return float(amount)

    @model_validator(mode="after")
    def parse_timestamp(self) -> "PosRow":
        try:
            self.timestamp = datetime.strptime(
                f"{self.order_date} {self.order_time}",
                "%d-%m-%Y %H:%M:%S",
            )
        except ValueError:
            raise ValueError("order_date and order_time must match DD-MM-YYYY HH:MM:SS") from None

        return self


class PosImportResult(BaseModel):
    processed_rows: int = 0
    imported_transactions: int = 0
    imported_items: int = 0
    skipped_transactions: int = 0
    failed_rows: int = 0
    errors: list[str] = Field(default_factory=list)
