from __future__ import annotations

from decimal import Decimal
from typing import Any

from paypal_sandbox_validation.diagnostics import classify_root_cause, decompose_case, infer_formula
from paypal_sandbox_validation.models import Case, CaseStatus
from paypal_sandbox_validation.quote_adapter import minor_units, quantize_currency


def _au_case(amount: str, fee: str, buyer: str = "US") -> Case:
    return Case(
        case_id=f"diag-AU-{buyer}-{amount}",
        run_id="r1",
        merchant_country="AU",
        buyer_country=buyer,
        amount=amount,
        currency="AUD",
        product_id="other_commercial",
        variant_id="standard",
        status=CaseStatus.RECONCILED,
        execution_classification="diagnostic_sandbox_pricing",
        paypal_evidence={
            "status": "COMPLETED",
            "gross_amount": {"value": amount, "currency_code": "AUD"},
            "paypal_fee": {"value": fee, "currency_code": "AUD"},
            "net_amount": {"value": str(Decimal(amount) - Decimal(fee)), "currency_code": "AUD"},
            "payer_country": buyer,
        },
    )


def _au_quote(base_pct: str, surcharge_pct: str | None = None) -> dict:
    amount = Decimal("10.00")
    base_total = quantize_currency(amount * Decimal(base_pct) / Decimal("100") + Decimal("0.30"), "AUD")
    surcharge_amount = (
        quantize_currency(amount * Decimal(surcharge_pct) / Decimal("100"), "AUD") if surcharge_pct else None
    )
    total = base_total + (surcharge_amount or Decimal("0"))
    components: list[dict[str, Any]] = [
        {
            "type": "processing",
            "amount": str(base_total),
            "currency": "AUD",
            "rate_percentage": base_pct,
            "fixed_amount": "0.30",
        }
    ]
    if surcharge_amount is not None:
        components.append(
            {
                "type": "surcharge",
                "amount": str(surcharge_amount),
                "currency": "AUD",
                "rate_percentage": surcharge_pct,
            }
        )
    return {
        "status": "exact_for_public_rate",
        "amount": {"value": "10.00", "currency": "AUD"},
        "processing_fee": {"value": str(total), "currency": "AUD"},
        "gross_amount": {"value": "10.00", "currency": "AUD"},
        "net_amount": {"value": str(amount - total), "currency": "AUD"},
        "components": components,
        "_schedule_metadata": {
            "base_percentage": base_pct,
            "fixed_amount": "0.30",
            "surcharge_percentage": surcharge_pct,
            "payer_region": "OTHER",
        },
        "data": {"content_sha256": "sha", "data_ref": "local"},
    }


def test_au_one_usd_international_remains_provisional() -> None:
    """A single AUD 1.00 US observation cannot discriminate formulas."""
    observations = [
        {
            "amount": "1.00",
            "currency": "AUD",
            "paypal_fee": "0.33",
            "buyer_country": "US",
            "observed_payer_country": "US",
        }
    ]
    formula = infer_formula(observations)
    assert formula["stable_linear_formula_found"] is False
    assert formula.get("best") is None or not formula["best"].get("fit")


def test_au_ten_usd_discriminates_four_candidate_formulas() -> None:
    """At AUD 10.00 only the account base + surcharge formula matches the observed US fee."""
    amount = Decimal("10.00")
    observed_minor = minor_units("0.64", "AUD")

    candidates = [
        ("public_base_no_surcharge", Decimal("2.9"), None),
        ("public_base_plus_surcharge", Decimal("2.9"), Decimal("1.00")),
        ("account_base_no_surcharge", Decimal("2.40"), None),
        ("account_base_plus_surcharge", Decimal("2.40"), Decimal("1.00")),
    ]

    matches = []
    for name, base_pct, surcharge_pct in candidates:
        base = amount * base_pct / Decimal("100")
        fixed = Decimal("0.30")
        surcharge = amount * surcharge_pct / Decimal("100") if surcharge_pct else Decimal("0")
        predicted = quantize_currency(base + fixed + surcharge, "AUD")
        if minor_units(predicted, "AUD") == observed_minor:
            matches.append(name)

    assert matches == ["account_base_plus_surcharge"]


def test_no_surcharge_classification() -> None:
    """When PayPal applies the base rate but omits the surcharge, the root cause is data defect."""
    case = _au_case("10.00", "0.59", buyer="US")
    case.quote = _au_quote("2.9", surcharge_pct="1.00")
    decomposition = decompose_case(case)
    formula = infer_formula(
        [
            {
                "amount": "10.00",
                "currency": "AUD",
                "paypal_fee": "0.59",
                "buyer_country": "US",
                "observed_payer_country": "US",
            }
        ],
        base_pct=Decimal("2.9"),
        surcharge_pct=Decimal("1.00"),
        fixed=Decimal("0.30"),
    )
    root_cause = classify_root_cause(case, decomposition, formula)
    assert root_cause["category"] == "payment_fee_data_defect"
    assert "surcharge" in root_cause["explanation"].lower()


def test_plus_one_pp_surcharge_classification() -> None:
    """When PayPal applies the total base-plus-1.00pp surcharge, the surcharge is rolled into the fee."""
    case = _au_case("10.00", "0.69", buyer="US")
    case.quote = _au_quote("2.9", surcharge_pct="1.00")
    decomposition = decompose_case(case)
    formula = infer_formula(
        [
            {
                "amount": "10.00",
                "currency": "AUD",
                "paypal_fee": "0.69",
                "buyer_country": "US",
                "observed_payer_country": "US",
            }
        ],
        base_pct=Decimal("2.9"),
        surcharge_pct=Decimal("1.00"),
        fixed=Decimal("0.30"),
    )
    root_cause = classify_root_cause(case, decomposition, formula)
    assert root_cause["category"] == "payment_fee_data_defect"
    assert "1.00pp" in root_cause["explanation"].lower() or "surcharge" in root_cause["explanation"].lower()
