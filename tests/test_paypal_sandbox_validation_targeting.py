from __future__ import annotations

import pytest
from click.exceptions import UsageError
from paypal_sandbox_validation.cli import (
    _filter_representative_merchants,
    _parse_requested_merchants,
    _select_regional_pilot_merchants,
)
from paypal_sandbox_validation.models import Account, AccountType, QualificationStatus
from paypal_sandbox_validation.qualification import (
    build_validation_plan,
    is_diagnostic_sandbox_pricing_merchant,
)
from paypal_sandbox_validation.quote_adapter import QuoteAdapter


def _merchant_accounts(*countries: str) -> dict[str, Account]:
    return {
        c: Account(
            country_code=c,
            account_type=AccountType.BUSINESS,
            primary_email_alias=f"{c.lower()}@example.com",
            password="x",
            first_name="Test",
            last_name="Merchant",
        )
        for c in countries
    }


def test_parse_requested_merchants_allows_single_alias() -> None:
    assert _parse_requested_merchants(None, "us") == ["US"]


def test_parse_requested_merchants_comma_list() -> None:
    assert _parse_requested_merchants("US,CA,gb", None) == ["US", "CA", "GB"]


def test_parse_requested_merchants_rejects_both() -> None:
    with pytest.raises(UsageError):
        _parse_requested_merchants("US,CA", "US")


def test_filter_representative_merchants_defaults_to_representative() -> None:
    registry = {
        "US": {"status": QualificationStatus.REPRESENTATIVE},
        "CA": {"status": QualificationStatus.REPRESENTATIVE},
        "DE": {"status": QualificationStatus.SANDBOX_SPECIFIC_PRICING},
    }
    selected = _filter_representative_merchants(None, _merchant_accounts("US", "CA", "DE"), registry)
    assert selected == ["CA", "US"]


def test_filter_representative_merchants_explicit_only_requested() -> None:
    registry = {
        "US": {"status": QualificationStatus.REPRESENTATIVE},
        "CA": {"status": QualificationStatus.REPRESENTATIVE},
    }
    selected = _filter_representative_merchants(["US"], _merchant_accounts("US", "CA"), registry)
    assert selected == ["US"]


def test_filter_representative_merchants_rejects_non_representative() -> None:
    registry = {"DE": {"status": QualificationStatus.SANDBOX_SPECIFIC_PRICING}}
    with pytest.raises(UsageError):
        _filter_representative_merchants(["DE"], _merchant_accounts("DE"), registry)


def test_filter_representative_merchants_rejects_unknown() -> None:
    registry = {"US": {"status": QualificationStatus.REPRESENTATIVE}}
    with pytest.raises(UsageError):
        _filter_representative_merchants(["XX"], _merchant_accounts("US"), registry)


def test_validation_plan_with_explicit_merchant_only_us() -> None:
    adapter = QuoteAdapter()
    plan = build_validation_plan(
        run_id="r1",
        qualified_merchants=["US"],
        buyer_countries={"US", "CA", "AU"},
        adapter=adapter,
    )
    merchants = {c.merchant_country for c in plan}
    assert merchants == {"US"}
    assert any(c.buyer_country == "US" for c in plan)
    assert any(c.buyer_country != "US" for c in plan)


def test_diagnostic_sandbox_pricing_eligibility() -> None:
    assert is_diagnostic_sandbox_pricing_merchant({"status": QualificationStatus.SANDBOX_SPECIFIC_PRICING})
    assert is_diagnostic_sandbox_pricing_merchant(
        {
            "status": QualificationStatus.INCONCLUSIVE,
            "manual_send_to_business": QualificationStatus.SANDBOX_SPECIFIC_PRICING.value,
        }
    )
    assert is_diagnostic_sandbox_pricing_merchant(
        {
            "status": QualificationStatus.INCONCLUSIVE,
            "manual_send_observation": {"classification": "sandbox_account_pricing_difference"},
        }
    )
    assert not is_diagnostic_sandbox_pricing_merchant({"status": QualificationStatus.ACCOUNT_CONFIGURATION_BLOCKED})
    assert not is_diagnostic_sandbox_pricing_merchant({"status": QualificationStatus.DATASET_NOT_CALCULABLE})
    assert not is_diagnostic_sandbox_pricing_merchant({"status": QualificationStatus.CAPABILITY_UNAVAILABLE})
    assert not is_diagnostic_sandbox_pricing_merchant({"status": QualificationStatus.SANDBOX_CHECKOUT_LIMITATION})
    assert not is_diagnostic_sandbox_pricing_merchant({"status": "compliance_violation"})
    assert not is_diagnostic_sandbox_pricing_merchant({"status": QualificationStatus.INCONCLUSIVE})


def test_diagnostic_sandbox_pricing_registry_sample() -> None:
    registry = {
        "DE": {
            "status": QualificationStatus.SANDBOX_SPECIFIC_PRICING,
            "representative_for_public_rates": False,
        },
        "AU": {"status": QualificationStatus.INCONCLUSIVE},
        "ES": {"status": "compliance_violation"},
        "CZ": {"status": QualificationStatus.DATASET_NOT_CALCULABLE},
        "GB": {"status": QualificationStatus.SANDBOX_SPECIFIC_PRICING},
    }
    eligible = {m for m in registry if is_diagnostic_sandbox_pricing_merchant(registry[m])}
    assert eligible == {"DE", "GB"}


def test_select_regional_pilot_merchants_explicit_us_only() -> None:
    registry = {
        "US": {"status": QualificationStatus.REPRESENTATIVE},
        "CA": {"status": QualificationStatus.REPRESENTATIVE},
        "DE": {"status": QualificationStatus.SANDBOX_SPECIFIC_PRICING},
    }
    selected = _select_regional_pilot_merchants(
        requested=["US"],
        configured_merchants={"US", "CA", "DE"},
        registry=registry,
        include_unsuitable=False,
        diagnostic_sandbox_pricing=False,
    )
    assert selected == ["US"]


def test_select_regional_pilot_merchants_diagnostic_allows_inconclusive_explicit() -> None:
    """An explicit diagnostic merchant may be inconclusive so it can be classified."""
    registry = {
        "AU": {"status": QualificationStatus.INCONCLUSIVE},
    }
    selected = _select_regional_pilot_merchants(
        requested=["AU"],
        configured_merchants={"AU"},
        registry=registry,
        include_unsuitable=False,
        diagnostic_sandbox_pricing=True,
    )
    assert selected == ["AU"]


def test_select_regional_pilot_merchants_diagnostic_rejects_blocked_explicit() -> None:
    registry = {
        "ES": {"status": "compliance_violation"},
    }
    with pytest.raises(UsageError):
        _select_regional_pilot_merchants(
            requested=["ES"],
            configured_merchants={"ES"},
            registry=registry,
            include_unsuitable=False,
            diagnostic_sandbox_pricing=True,
        )
