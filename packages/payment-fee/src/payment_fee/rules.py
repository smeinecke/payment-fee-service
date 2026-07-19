from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _normalize_confidence(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


class ExecutableFeeRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    label: str
    component_type: str = "processing"
    behavior: str = "base"
    percentage: Decimal | None = None
    basis_points: Decimal | None = None
    fixed_amount: Decimal | None = None
    fixed_currency: str | None = None
    minimum_amount: Decimal | None = None
    maximum_amount: Decimal | None = None
    currency: str | None = None
    payer: str | None = None
    unit: str | None = None
    classification_status: str = "calculable"
    confidence: int | float | None = None
    exactness: str | None = None
    source_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_whole_number(cls, value):
        return _normalize_confidence(value)


class CompiledFeePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    market: str
    currency: str
    rules: list[ExecutableFeeRule]
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    schema_version: int = 1
    content_sha256: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    source_updated_at: str | None = None
    data_ref: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
