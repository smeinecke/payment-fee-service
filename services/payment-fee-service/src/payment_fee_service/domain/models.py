from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    currency: str = Field(min_length=3, max_length=3)


class PayPalPayment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_type: str = "standard_commercial"
    surcharge_region: str | None = None


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
    context: dict[str, Any] = Field(default_factory=dict)


class BaseQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Money
    account_country: str = Field(min_length=2, max_length=2)
    customer_country: str | None = Field(default=None, min_length=2, max_length=2)
    settlement_currency: str | None = Field(default=None, min_length=3, max_length=3)


class PayPalQuoteRequest(BaseQuoteRequest):
    provider: str = "paypal"
    payment: PayPalPayment


class StripeQuoteRequest(BaseQuoteRequest):
    provider: str = "stripe"
    payment: StripePayment
