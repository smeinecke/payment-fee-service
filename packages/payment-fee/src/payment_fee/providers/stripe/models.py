from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StripeFeeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    operator: str = "eq"
    value: Any
    evidence: Any | None = None


class StripeFeeComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    amount: str | None = None
    basis_points: str | None = None
    currency: str | None = None
    minor_amount: str | None = None
    operator: str | None = None
    schedule_id: str | None = None
    source_entry_id: str | None = None
    source_text: str | None = None
    value: str | None = None


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
    basis_points: str | None = None
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
    fixed_amount: str | None = None
    fixed_amount_minor: str | None = None
    fixed_currency: str | None = None
    integration_type: str | None = None
    maximum_amount: str | None = None
    minimum_amount: str | None = None
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
    transaction_amount_max: str | None = None
    transaction_amount_min: str | None = None
    transaction_region: str | None = None
    transaction_type: str | None = None
    payment_method_variant: str | None = None
    bank_account_validation: str | None = None


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


class StripePaymentMethodName(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str
    name: str


class StripePaymentMethodEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method_id: str
    family: str
    display_name: str
    fee_rule_refs: list[str] = Field(default_factory=list)
    localized_names: list[StripePaymentMethodName] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    supported_account_countries: list[str] = Field(default_factory=list)


class StripePaymentMethods(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    generated_at: str | None = None
    methods: list[StripePaymentMethodEntry] = Field(default_factory=list)
