from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PayPalFeeComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    value: str | None = None
    amount: str | None = None
    currency: str | None = None
    schedule_id: str | None = None


class PayPalSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_url: str | None = None
    classifier_version: str | None = None
    component_id: str | None = None
    document_id: str | None = None
    page_id: str | None = None
    page_title: str | None = None
    requested_url: str | None = None
    row_id: str | None = None
    row_index: int | None = None
    section_heading: str | None = None
    table_id: str | None = None
    original_label: str | None = None


class PayPalTransactionFeeRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    variant_id: str | None = None
    label: str | None = None
    percentage: str | None = None
    fixed_fee_schedule: str | None = None
    international_surcharge_schedule: str | None = None
    maximum_fee_schedule: str | None = None
    rate_reference: dict[str, Any] | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    calculation_status: str = "calculable"
    fee_components: list[PayPalFeeComponent] = Field(default_factory=list)
    source: PayPalSource | None = None


class PayPalFixedFeeSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: dict[str, str] = Field(default_factory=dict)


class PayPalInternationalSurchargeEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payer_region: str
    percentage_points: str | None = None


class PayPalInternationalSurchargeSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[PayPalInternationalSurchargeEntry] = Field(default_factory=list)


class PayPalMaximumFeeSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: dict[str, str] = Field(default_factory=dict)


class PayPalCurrencyConversion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spread_percentage: str | None = None


class PayPalDerivedData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "unclassified"
    transaction_fee_rules: list[PayPalTransactionFeeRule] = Field(default_factory=list)
    fixed_fee_schedules: dict[str, PayPalFixedFeeSchedule] = Field(default_factory=dict)
    international_surcharge_schedules: dict[str, PayPalInternationalSurchargeSchedule] = Field(default_factory=dict)
    maximum_fee_schedules: dict[str, PayPalMaximumFeeSchedule] = Field(default_factory=dict)
    currency_conversion: PayPalCurrencyConversion | None = None


class PayPalCountryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country_code: str
    iso_country_code: str | None = None
    paypal_market_code: str | None = None
    derived_status: str = "unclassified"
    derived: PayPalDerivedData = Field(default_factory=PayPalDerivedData)


class PayPalCoreFees(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    generated_at: str | None = None
    countries: list[PayPalCountryEntry] = Field(default_factory=list)


class PayPalIndexCountry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country_code: str
    iso_country_code: str | None = None
    paypal_market_code: str | None = None
    derived_status: str = "unclassified"
    locale: str | None = None
    data_url: str | None = None
    content_sha256: str | None = None
    source_url: str | None = None
    source_updated_at: str | None = None
    crawled_at: str | None = None


class PayPalIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    generated_at: str | None = None
    countries: list[PayPalIndexCountry] = Field(default_factory=list)
