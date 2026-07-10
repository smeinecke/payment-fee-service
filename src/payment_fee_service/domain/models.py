from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
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


class PayPalTransactionType(StrEnum):
    STANDARD_COMMERCIAL = "standard_commercial"
    GOODS_AND_SERVICES = "goods_and_services"
    MICROPAYMENTS = "micropayments"
    DONATIONS = "donations"
    NONPROFIT = "nonprofit"


class PayPalPayment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_type: PayPalTransactionType = PayPalTransactionType.STANDARD_COMMERCIAL
    surcharge_region: str | None = Field(
        default=None,
        description=(
            "PayPal fee-region label from the dataset, required for an international payer "
            "when it cannot be resolved safely from a country code."
        ),
    )

    @field_validator("surcharge_region")
    @classmethod
    def uppercase_region(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class StripeCardContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str | None = None
    region: str | None = None
    tier: str | None = None


class StripePayment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    channel: str | None = None
    recurring: bool | None = None
    billing_type: str | None = None
    currency_conversion_required: bool | None = None
    card: StripeCardContext | None = None
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional Stripe rule dimensions used by published FeeCondition entries.",
    )


class BaseQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Money
    account_country: str = Field(min_length=2, max_length=2)
    customer_country: str | None = Field(default=None, min_length=2, max_length=2)
    settlement_currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("account_country", "customer_country")
    @classmethod
    def uppercase_country(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @field_validator("settlement_currency")
    @classmethod
    def uppercase_settlement_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class PayPalQuoteRequest(BaseQuoteRequest):
    provider: Literal["paypal"]
    payment: PayPalPayment


class StripeQuoteRequest(BaseQuoteRequest):
    provider: Literal["stripe"]
    payment: StripePayment


QuoteRequest = Annotated[
    PayPalQuoteRequest | StripeQuoteRequest,
    Field(discriminator="provider"),
]


class FeeComponent(BaseModel):
    type: str
    label: str
    amount: Decimal
    currency: str
    rate_percentage: Decimal | None = None
    fixed_amount: Decimal | None = None
    source_rule_id: str


class MatchedRule(BaseModel):
    rule_id: str
    classification_status: str
    confidence: float | None = None
    exactness: str | None = None
    source_url: str | None = None


class DataProvenance(BaseModel):
    provider: str
    schema_version: int
    market: str
    content_sha256: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    source_updated_at: str | None = None
    data_ref: str | None = None


class QuoteResponse(BaseModel):
    provider: str
    status: Literal["exact_for_public_rate", "estimated"]
    amount: Money
    processing_fee: Money
    net_amount: Money
    components: list[FeeComponent]
    matched_rules: list[MatchedRule]
    assumptions: list[str] = Field(default_factory=list)
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
    provider: str
    account_country: str
    market_code: str
    locale: str | None = None
    status: str
    source_urls: list[str] = Field(default_factory=list)


class ProviderInfo(BaseModel):
    provider: str
    ready: bool
    market_count: int
    error: str | None = None


class CapabilityInfo(BaseModel):
    provider: str
    account_country: str
    quotable: bool
    transaction_types: list[str] = Field(default_factory=list)
    payment_methods: list[str] = Field(default_factory=list)
    required_context: list[str] = Field(default_factory=list)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody
