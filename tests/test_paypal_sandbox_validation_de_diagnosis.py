"""Tests for PayPal Sandbox DE checkout diagnosis helpers."""

from __future__ import annotations

from paypal_sandbox_validation.diagnostics import classify_de_checkout_outcome
from paypal_sandbox_validation.paypal_api import (
    build_order_payload,
    extract_payee_info,
    order_payload_signature,
)


def test_extract_payee_info_from_order() -> None:
    order = {
        "purchase_units": [
            {
                "payee": {
                    "email_address": "merchant@example.com",
                    "merchant_id": "ABC123",
                }
            }
        ]
    }
    info = extract_payee_info(order)
    assert info["email_address"] == "merchant@example.com"
    assert info["merchant_id"] == "ABC123"


def test_extract_payee_info_missing() -> None:
    assert extract_payee_info({}) == {"email_address": None, "merchant_id": None}


def test_order_payload_variants() -> None:
    app_ctx = build_order_payload(
        amount="1.00",
        currency="EUR",
        return_url="http://127.0.0.1:1234/paypal/return",
        cancel_url="http://127.0.0.1:1234/paypal/cancel",
        reference_id="ref-1",
        invoice_id="inv-1",
        custom_id="cust-1",
        form="application_context",
    )
    assert "application_context" in app_ctx
    assert "payment_source" not in app_ctx
    assert app_ctx["application_context"]["shipping_preference"] == "NO_SHIPPING"

    ps = build_order_payload(
        amount="1.00",
        currency="EUR",
        return_url="http://127.0.0.1:1234/paypal/return",
        cancel_url="http://127.0.0.1:1234/paypal/cancel",
        reference_id="ref-1",
        invoice_id="inv-1",
        custom_id="cust-1",
        form="payment_source",
    )
    assert "payment_source" in ps
    assert "application_context" not in ps
    assert ps["payment_source"]["paypal"]["experience_context"]["shipping_preference"] == "NO_SHIPPING"


def test_order_payload_signature_secret_free() -> None:
    payload = build_order_payload(
        amount="1.00",
        currency="EUR",
        return_url="http://127.0.0.1:1234/paypal/return",
        cancel_url="http://127.0.0.1:1234/paypal/cancel",
        reference_id="ref-1",
        invoice_id="inv-1",
        custom_id="cust-1",
    )
    sig = order_payload_signature(payload)
    assert sig["form"] == "application_context"
    assert sig["amount"] == "1.00"
    assert sig["purchase_unit_count"] == 1
    assert sig["return_url_present"] is True
    assert sig["cancel_url_present"] is True
    assert "return_url" not in sig
    assert "cancel_url" not in sig


def test_classify_association_mismatch() -> None:
    result = classify_de_checkout_outcome(
        association_verified=False,
        manual_send_money={"succeeded": True, "transaction_type": "unknown"},
        manual_order={"status": "timeout"},
        playwright_results=[{"status": "failed", "issue": "COMPLIANCE_VIOLATION"}],
    )
    assert result["status"] == "rest_credentials_merchant_mismatch"


def test_classify_b_success_c_failure() -> None:
    result = classify_de_checkout_outcome(
        association_verified=True,
        manual_send_money={"succeeded": True, "transaction_type": "unknown"},
        manual_order={"status": "approved"},
        playwright_results=[{"status": "failed", "issue": "COMPLIANCE_VIOLATION"}],
    )
    assert result["status"] == "playwright_automation_defect"


def test_classify_b_and_c_success() -> None:
    result = classify_de_checkout_outcome(
        association_verified=True,
        manual_send_money={"succeeded": True, "transaction_type": "unknown"},
        manual_order={"status": "approved"},
        playwright_results=[{"status": "approved"}],
    )
    assert result["status"] == "transient_sandbox_error"


def test_classify_payload_defect() -> None:
    result = classify_de_checkout_outcome(
        association_verified=True,
        manual_send_money={"succeeded": True, "transaction_type": "unknown"},
        manual_order={"status": "timeout"},
        playwright_results=[
            {"status": "approved", "payload_variant": "application_context"},
            {"status": "failed", "payload_variant": "payment_source", "issue": "COMPLIANCE_VIOLATION"},
        ],
    )
    assert result["status"] == "orders_v2_payload_defect"


def test_classify_sandbox_checkout_limitation() -> None:
    result = classify_de_checkout_outcome(
        association_verified=True,
        manual_send_money={"succeeded": True, "transaction_type": "unknown"},
        manual_order={"status": "timeout"},
        playwright_results=[
            {"status": "failed", "payload_variant": "application_context", "issue": "COMPLIANCE_VIOLATION"},
            {"status": "failed", "payload_variant": "payment_source", "issue": "COMPLIANCE_VIOLATION"},
        ],
    )
    assert result["status"] == "sandbox_checkout_limitation"


def test_classify_account_configuration_difference_when_send_money_also_fails() -> None:
    result = classify_de_checkout_outcome(
        association_verified=True,
        manual_send_money={"succeeded": False, "transaction_type": "unknown"},
        manual_order={"status": "timeout"},
        playwright_results=[
            {"status": "failed", "payload_variant": "application_context", "issue": "COMPLIANCE_VIOLATION"},
        ],
    )
    assert result["status"] == "account_configuration_difference"
