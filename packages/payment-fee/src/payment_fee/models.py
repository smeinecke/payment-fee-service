from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return value.upper()


class CardContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str | None = None
    region: str | None = None
    type: str | None = None
    network: str | None = None
    tier: str | None = None
    entry_mode: str | None = None


class SettlementContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str | None = Field(default=None, min_length=3, max_length=3)
    timing: str | None = None

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class BankContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validation: str | None = None
    transfer_type: str | None = None


class BaseTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str | None = None
    variant_id: str | None = None
    payment_method: str | None = None
    payment_method_variant: str | None = None
    channel: str | None = None
    pricing_plan: str | None = None
    pricing_tier: str | None = None
    payer: str | None = None
    unit: str | None = "per_transaction"
    currency_conversion_required: bool | None = None
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("context")
    @classmethod
    def _context_values_are_json_scalar_or_array(cls, value: dict[str, Any]) -> dict[str, Any]:
        for k, v in value.items():
            if isinstance(v, dict):
                raise ValueError(
                    f"Context value for {k!r} must not be a nested object. "
                    "Use a typed transaction field for structured data."
                )
            if not isinstance(v, (str, int, float, bool, type(None), list)):
                raise ValueError(f"Context value for {k!r} must be a JSON scalar or array.")
            if isinstance(v, list):
                for item in v:
                    if not isinstance(item, (str, int, float, bool, type(None))):
                        raise ValueError(f"Context array value for {k!r} must contain only JSON scalars.")
        return value


class PayPalTransaction(BaseTransaction):
    transaction_region: str | None = None
    payer_region: str | None = None
    surcharge_region: str | None = None
    merchant_approval_required: bool | None = None
    withdrawal_method: str | None = None
    authorization_channel: str | None = None
    point_of_sale: bool | None = None
    card_present: bool | None = None
    transaction_purpose: str | None = None
    funding_source: str | None = None
    service: str | None = None
    recipient_location: str | None = None
    volume_status: str | None = None
    fee_currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("fee_currency")
    @classmethod
    def uppercase_fee_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class StripeTransaction(BaseTransaction):
    card: CardContext | None = None
    settlement: SettlementContext | None = None
    bank: BankContext | None = None
    recurring: bool | None = None
    billing_type: str | None = None
    transaction_region: str | None = None
    customer_country: str | None = Field(default=None, min_length=2, max_length=2)
    presentment_currency: str | None = Field(default=None, min_length=3, max_length=3)
    settlement_currency: str | None = Field(default=None, min_length=3, max_length=3)
    settlement_timing: str | None = None
    bank_account_validation: str | None = None
    integration_type: str | None = None
    product_feature: str | None = None
    contract_length: str | None = None
    cross_border: bool | None = None
    feature_enabled: str | None = None
    dispute_state: str | None = None
    card_tier: str | None = None
    card_type: str | None = None
    card_network: str | None = None
    card_origin: str | None = None
    card_region: str | None = None
    card_entry_mode: str | None = None

    @field_validator("customer_country", "presentment_currency", "settlement_currency")
    @classmethod
    def uppercase_fields(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class BaseQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    amount: Money
    account_country: str = Field(min_length=2, max_length=2)
    customer_country: str | None = Field(default=None, min_length=2, max_length=2)
    settlement_currency: str | None = Field(default=None, min_length=3, max_length=3)
    transaction: BaseTransaction

    @field_validator("account_country", "customer_country")
    @classmethod
    def uppercase_country(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @field_validator("settlement_currency")
    @classmethod
    def uppercase_settlement_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class PayPalQuoteRequest(BaseQuoteRequest):
    provider: Literal["paypal"] = "paypal"
    transaction: PayPalTransaction


class StripeQuoteRequest(BaseQuoteRequest):
    provider: Literal["stripe"] = "stripe"
    transaction: StripeTransaction


QuoteRequest = Annotated[
    PayPalQuoteRequest | StripeQuoteRequest,
    Field(discriminator="provider"),
]


class FeeComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    label: str
    amount: Decimal
    currency: str
    rate_percentage: Decimal | None = None
    fixed_amount: Decimal | None = None
    minimum_applied: bool = False
    maximum_applied: bool = False
    payer: str | None = None
    unit: str | None = None
    source_rule_id: str


class MatchedRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    classification_status: str
    confidence: float | None = None
    exactness: str | None = None
    source_url: str | None = None


class DataProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    schema_version: int
    market: str
    content_sha256: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    source_updated_at: str | None = None
    data_ref: str | None = None


class QuoteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    status: Literal["exact_for_public_rate", "estimated", "range", "included", "not_calculable"]
    amount: Money
    processing_fee: Money
    net_amount: Money
    components: list[FeeComponent]
    matched_rules: list[MatchedRule]
    selected_product_id: str | None = None
    selected_variant_id: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    data: DataProvenance

    @model_validator(mode="after")
    def totals_are_consistent(self) -> QuoteResponse:
        total = sum((component.amount for component in self.components), Decimal("0"))
        if total != self.processing_fee.value:
            raise ValueError("processing_fee must equal the sum of components")
        if self.amount.value - self.processing_fee.value != self.net_amount.value:
            raise ValueError("net_amount must equal amount minus processing_fee")
        return self


class MarketInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    account_country: str
    market_code: str
    locale: str | None = None
    status: str
    source_urls: list[str] = Field(default_factory=list)


class ProviderInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    ready: bool
    market_count: int = 0
    error: str | None = None


class CapabilityInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    account_country: str
    quotable: bool
    product_ids: list[str] = Field(default_factory=list)
    variants: list[str] = Field(default_factory=list)
    payment_methods: list[str] = Field(default_factory=list)
    supported_fee_shapes: list[str] = Field(default_factory=list)
    supported_currencies: list[str] = Field(default_factory=list)
    condition_dimensions: list[str] = Field(default_factory=list)
    allowed_values: dict[str, list[Any]] = Field(default_factory=dict)
    required_context: list[str] = Field(default_factory=list)
    calculable_products: dict[str, list[str]] = Field(default_factory=dict)
    dataset_status: str = "unknown"
    source_revision: str | None = None


class QuoteSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    account_country: str
    request_schema: dict[str, Any] = Field(default_factory=dict)
    response_schema: dict[str, Any] = Field(default_factory=dict)
