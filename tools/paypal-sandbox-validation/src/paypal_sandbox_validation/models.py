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
    nvp_user: str | None = Field(default=None, repr=False, exclude=True)
    nvp_password: str | None = Field(default=None, repr=False, exclude=True)
    nvp_signature: str | None = Field(default=None, repr=False, exclude=True)

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


class QualificationStatus(StrEnum):
    REPRESENTATIVE = "representative"
    SANDBOX_SPECIFIC_PRICING = "sandbox_specific_pricing"
    SANDBOX_PROFILE_PRICING_CONFIRMED = "sandbox_profile_pricing_confirmed"
    ACCOUNT_CONFIGURATION_BLOCKED = "account_configuration_blocked"
    DATASET_NOT_CALCULABLE = "dataset_not_calculable"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    INCONCLUSIVE = "inconclusive"
    UNDER_INVESTIGATION = "under_investigation"
    SANDBOX_CHECKOUT_LIMITATION = "sandbox_checkout_limitation"
    ORDERS_V2_PAYLOAD_DEFECT = "orders_v2_payload_defect"
    PLAYWRIGHT_AUTOMATION_DEFECT = "playwright_automation_defect"
    REST_CREDENTIALS_MISMATCH = "rest_credentials_merchant_mismatch"
    TRANSIENT_SANDBOX_ERROR = "transient_sandbox_error"


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
    retry_failed: bool = False
    dry_run: bool = False
    confirm_full_matrix: bool = False
    payload_variant: str = "application_context"


class CaseStatus(StrEnum):
    PLANNED = "planned"
    PREDICTION_READY = "prediction_ready"
    ORDER_CREATED = "order_created"
    BUYER_APPROVED = "buyer_approved"
    BUYER_REVIEW_READY = "buyer_review_ready"
    PAYMENT_SUBMITTED = "payment_submitted"
    MERCHANT_TRANSACTION_FOUND = "merchant_transaction_found"
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
    execution_path: str = "orders_v2_checkout"
    product_id: str
    variant_id: str
    status: CaseStatus = CaseStatus.PLANNED
    manual_state: str | None = None
    manual_payment_type: str | None = None
    funding_source: str | None = None
    buyer_ui_evidence: dict[str, Any] | None = None
    merchant_ui_evidence: dict[str, Any] | None = None
    request_id_create: str | None = None
    request_id_capture: str | None = None
    create_attempts: int = 0
    capture_attempts: int = 0
    paypal_operations_executed_in_current_run: int = 0
    observation_source: str | None = None
    order_id: str | None = None
    approval_url: str | None = None
    capture_id: str | None = None
    payer_id: str | None = Field(default=None, repr=False, exclude=True)
    observed_payer_country: str | None = None
    expected_payer_region: str | None = None
    expected_surcharge_components: int = 0
    expected_surcharge_amount: str | None = None
    quote: dict[str, Any] | None = None
    paypal_evidence: dict[str, Any] | None = None
    reconciliation: dict[str, Any] | None = None
    paypal_error: dict[str, Any] | None = None
    paypal_issue: str | None = None
    paypal_operation: str | None = None
    paypal_debug_id: str | None = None
    nvp_transaction_id: str | None = Field(default=None, repr=False, exclude=True)
    evidence_source: str | None = None
    manual_submitted_at: str | None = None
    product_selection_source: str = "explicit_execution_path_mapping"
    prediction_provenance: str = "legacy_prediction_unknown"
    prediction_created_before_original_submission: bool = False
    prediction_created_before_observation_reuse: bool = False
    original_submission_timestamp_known: bool = False
    prediction_sha256: str | None = None
    prediction_created_at: str | None = None
    prediction_unchanged_after_observation: bool | None = None
    execution_classification: str = "public_rate_validation"
    planning_time_registry_status: str | None = None
    pilot_metadata: dict[str, Any] = Field(default_factory=dict)


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
    AUTHENTICATION_FAILED = "authentication_failed"
    ACCOUNT_CAPABILITY_UNAVAILABLE = "account_capability_unavailable"
    ACCOUNT_CONFIGURATION_DIFFERENCE = "account_configuration_difference"
    NO_DISTINCT_FEE_SCHEDULE_CANDIDATE = "no_distinct_fee_schedule_candidate"
    EXCLUDED_FX_CASE = "excluded_fx_case"
    MERCHANT_TRANSACTION_NOT_FOUND = "merchant_transaction_not_found"
    MERCHANT_TRANSACTION_AMBIGUOUS = "merchant_transaction_ambiguous"
    FUNDING_SOURCE_NOT_SUPPORTED = "funding_source_not_supported_for_validation"
    UNSUPPORTED_PAYPAL_UI_STATE = "unsupported_paypal_ui_state"
    RECIPIENT_MISMATCH = "recipient_mismatch"
    TRANSACTION_TYPE_MISMATCH = "transaction_type_mismatch"
    UNSUPPORTED_PAYMENT_TYPE = "unsupported_payment_type"
    INCOMPLETE_PAYMENT = "incomplete_payment"
    PREDICTION_CHANGED = "prediction_changed"
    HISTORICAL_OBSERVATION_CURRENT_MISMATCH = "historical_observation_current_mismatch"


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
    base_rule_id: str | None = None
    fixed_fee_schedule_id: str | None = None
    international_surcharge_schedule_id: str | None = None
    base_percentage: str | None = None
    fixed_amount: str | None = None
    surcharge_percentage: str | None = None
    predicted_total_fee: str | None = None
    payer_region: str | None = None
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
    net_amount_mismatches: int = 0
    currency_mismatches: int = 0
    buyer_country_mismatches: int = 0
    library_not_calculable: int = 0
    blocked_buyer_interactions: int = 0
    api_failures: int = 0
    capability_exclusions: int = 0
    configuration_exclusions: int = 0
    pending: int = 0
    merchants_present: int = 0
    merchants_valid: int = 0
    merchants_probed: int = 0
    oauth_successful: int = 0
    oauth_failed: int = 0
    oauth_skipped: int = 0
    dataset_revision: str | None = None
    payment_fee_commit: str | None = None
    cases: list[dict[str, Any]] = Field(default_factory=list)
