from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from payment_fee.calculator import to_decimal


class StripeFeeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    operator: str = "eq"
    value: Any
    evidence: Any | None = None


class StripeFeeComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    amount: Decimal | None = None
    basis_points: Decimal | None = None
    currency: str | None = None
    minor_amount: Decimal | None = None
    operator: str | None = None
    schedule_id: str | None = None
    source_entry_id: str | None = None
    source_text: str | None = None
    value: Decimal | None = None

    @field_validator("amount", "basis_points", "minor_amount", "value", mode="before")
    @classmethod
    def _decimal_fields(cls, value: Any) -> Any:
        if value is None:
            return None
        return to_decimal(value, "fee component")


class StripeFeeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confidence: float = 0.0
    phrases: list[str] = Field(default_factory=list)
    source_entry_ids: list[str] = Field(default_factory=list)
    type: str | None = None


class StripeRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    provider: str = "stripe"
    account_country: str | None = None
    behavior: str = "additive"
    channel: str | None = None
    classification_status: str = "unclassified"
    conditions: list[StripeFeeCondition] = Field(default_factory=list)
    confidence: float = 0.0
    exactness: str = "exact"
    fee_components: list[StripeFeeComponent] = Field(default_factory=list)
    fee_evidence: StripeFeeEvidence | None = None
    label: str | None = None
    name: str | None = None
    payment_method: str | None = None
    product_id: str | None = None
    unit: str = "per_transaction"
    variant_id: str | None = None

    # Additional dimensions that may appear as top-level fields
    additional_fees: list[dict[str, Any]] = Field(default_factory=list)
    basis_points: Decimal | None = None
    billing_type: str | None = None
    card_network: str | None = None
    card_origin: str | None = None
    card_region: str | None = None
    card_tier: str | None = None
    card_type: str | None = None
    card_entry_mode: str | None = None
    contract_length: str | None = None
    cross_border: bool | None = None
    currency_conversion_required: bool | None = None
    customer_country: str | None = None
    dispute_state: str | None = None
    feature_enabled: str | None = None
    fee_type: str | None = None
    fixed_amount: Decimal | None = None
    fixed_amount_minor: Decimal | None = None
    fixed_currency: str | None = None
    integration_type: str | None = None
    maximum_amount: Decimal | None = None
    minimum_amount: Decimal | None = None
    payer: str | None = None
    presentment_currency: str | None = None
    pricing_plan: str | None = None
    pricing_tier: str | None = None
    product_feature: str | None = None
    recurring: bool | None = None
    settlement_currency: str | None = None
    settlement_timing: str | None = None
    source_url: str | None = None
    success: bool | None = None
    transaction_amount_max: Decimal | None = None
    transaction_amount_min: Decimal | None = None
    transaction_region: str | None = None
    transaction_type: str | None = None
    payment_method_variant: str | None = None
    bank_account_validation: str | None = None
    bank_transfer_type: str | None = None

    @field_validator(
        "basis_points",
        "fixed_amount",
        "fixed_amount_minor",
        "maximum_amount",
        "minimum_amount",
        "transaction_amount_max",
        "transaction_amount_min",
        mode="before",
    )
    @classmethod
    def _decimal_fields(cls, value: Any) -> Any:
        if value is None:
            return None
        return to_decimal(value, "rule")


class StripeMarketEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_country: str
    stripe_market_code: str | None = None
    locale: str | None = None
    derivation_status: str = "unclassified"
    calculator_coverage_status: str | None = None
    coverage_summary: dict[str, Any] | None = None
    unclassified_count: int = 0
    rules: list[StripeRule] = Field(default_factory=list)


class StripeCoreFees(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    generated_at: str | None = None
    markets: list[StripeMarketEntry] = Field(default_factory=list)


class StripeIndexMarket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_country: str
    stripe_market_code: str | None = None
    locale: str | None = None
    derivation_status: str = "unclassified"
    calculator_coverage_status: str | None = None
    data_path: str | None = None
    content_sha256: str | None = None
    source_updated_at: str | None = None
    source_urls: list[str] = Field(default_factory=list)


class StripeIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    generated_at: str | None = None
    markets: list[StripeIndexMarket] = Field(default_factory=list)
