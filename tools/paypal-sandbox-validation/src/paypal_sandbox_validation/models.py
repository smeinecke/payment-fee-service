from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AccountType(StrEnum):
    BUSINESS = "BUSINESS"
    PERSONAL = "PERSONAL"


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country_code: str
    account_type: AccountType
    primary_email_alias: str
    password: str = Field(..., repr=False, exclude=True)
    first_name: str
    last_name: str
    pp_balance: str = ""
    add_bank: str = ""
    cc_type: str = ""
    payment_card: str = Field(default="", repr=False, exclude=True)
    client_id: str | None = Field(default=None, repr=False, exclude=True)
    secret: str | None = Field(default=None, repr=False, exclude=True)

    def is_business(self) -> bool:
        return self.account_type == AccountType.BUSINESS

    def is_personal(self) -> bool:
        return self.account_type == AccountType.PERSONAL

    def __repr__(self) -> str:
        return (
            f"Account(country_code={self.country_code!r}, "
            f"account_type={self.account_type!r}, "
            f"primary_email_alias=<redacted>)"
        )


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts_csv: str
    profile: str = "smoke"
    merchant: str | None = None
    buyer: str | None = None
    case_id: str | None = None
    amount: str | None = None
    currency: str | None = None
    headful: bool = False
    headed: bool = False
    slow_mo: int = 0
    max_cases: int | None = None
    resume: str | None = None
    continue_after_mismatch: bool = False
    dry_run: bool = False
    confirm_full_matrix: bool = False


class CaseStatus(StrEnum):
    PLANNED = "planned"
    PREDICTION_READY = "prediction_ready"
    ORDER_CREATED = "order_created"
    BUYER_APPROVED = "buyer_approved"
    CAPTURED = "captured"
    RECONCILED = "reconciled"
    FAILED = "failed"
    SKIPPED = "skipped"


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    run_id: str
    merchant_country: str
    buyer_country: str
    amount: str
    currency: str
    product_id: str
    variant_id: str
    status: CaseStatus = CaseStatus.PLANNED
    request_id_create: str | None = None
    request_id_capture: str | None = None
    order_id: str | None = None
    approval_url: str | None = None
    capture_id: str | None = None
    quote: dict[str, Any] | None = None
    paypal_evidence: dict[str, Any] | None = None
    reconciliation: dict[str, Any] | None = None


class OAuthProbeStatus(StrEnum):
    SUCCESS = "success"
    INVALID_CLIENT = "invalid_client"
    AUTHENTICATION_FAILED = "authentication_failed"
    UNREACHABLE = "sandbox_endpoint_unreachable"
    TIMEOUT = "timeout"
    UNEXPECTED = "unexpected_response"


class OAuthProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: str
    status: OAuthProbeStatus
    expires_in: int | None = None
    scope_count: int | None = None
    classification: str | None = None


class ReconciliationStatus(StrEnum):
    MATCH = "match"
    FEE_MISMATCH = "fee_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    NET_AMOUNT_MISMATCH = "net_amount_mismatch"
    LIBRARY_NOT_CALCULABLE = "library_not_calculable"
    LIBRARY_MISSING_CONTEXT = "library_missing_context"
    LIBRARY_AMBIGUOUS = "library_ambiguous"
    PAYPAL_FEE_UNAVAILABLE = "paypal_fee_unavailable"
    BUYER_COUNTRY_MISMATCH = "buyer_country_mismatch"
    BUYER_INTERACTION_BLOCKED = "buyer_interaction_blocked"
    BUYER_CANCELLED = "buyer_cancelled"
    PAYPAL_API_FAILURE = "paypal_api_failure"
    ACCOUNT_CAPABILITY_UNAVAILABLE = "account_capability_unavailable"
    ACCOUNT_CONFIGURATION_DIFFERENCE = "account_configuration_difference"
    EXCLUDED_FX_CASE = "excluded_fx_case"


class ReconciliationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ReconciliationStatus
    delta_minor_units: int | None = None
    paypal_fee_value: str | None = None
    paypal_fee_currency: str | None = None
    library_fee_value: str | None = None
    library_fee_currency: str | None = None
    paypal_net_value: str | None = None
    library_net_value: str | None = None
    gross_currency: str | None = None
    gross_value: str | None = None
    paypal_fee_minor: int | None = None
    library_fee_minor: int | None = None
    components: list[dict[str, Any]] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    schedules: list[str] = Field(default_factory=list)
    merchant_country: str | None = None
    buyer_country: str | None = None
    observed_payer_country: str | None = None
    amount: str | None = None
    currency: str | None = None
    root_cause: str | None = None


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    planned: int = 0
    orders_created: int = 0
    buyer_approvals: int = 0
    completed_captures: int = 0
    matches: int = 0
    fee_mismatches: int = 0
    currency_mismatches: int = 0
    library_not_calculable: int = 0
    blocked_buyer_interactions: int = 0
    api_failures: int = 0
    capability_exclusions: int = 0
    cases: list[dict[str, Any]] = Field(default_factory=list)
