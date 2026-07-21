from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from paypal_sandbox_validation.models import ReconciliationStatus
from paypal_sandbox_validation.persistence import load_json, run_dir, save_json
from paypal_sandbox_validation.redaction import redact_path


def _dataset_revision(data_path: Path | None = None) -> str | None:
    if data_path is None:
        from paypal_sandbox_validation.quote_adapter import _data_path

        data_path = Path(_data_path())
    try:
        revision = load_json(data_path / "meta" / "crawler-revision.json")
        classifier = load_json(data_path / "meta" / "classifier-version.json")
        parts = [revision.get("crawler_revision"), classifier.get("classifier_version")]
        return "-".join(p for p in parts if p) or None
    except Exception:
        return None


def _payment_fee_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def _find_data_path(cases: list[dict[str, Any]]) -> Path | None:
    for case in cases:
        quote = case.get("quote") or {}
        path = quote.get("_data_path")
        if path:
            return Path(path)
    return None


def _quote_schedule_fields(quote: dict[str, Any] | None) -> dict[str, Any]:
    if not quote:
        return {}
    request = quote.get("_request", {})
    transaction = request.get("transaction", {})
    meta = quote.get("_schedule_metadata") or {}
    components = quote.get("components", []) or []
    return {
        "transaction_region": transaction.get("transaction_region"),
        "payer_region": transaction.get("payer_region") or meta.get("payer_region"),
        "product_id": quote.get("_scenario", {}).get("product_id"),
        "variant_id": quote.get("_scenario", {}).get("variant_id"),
        "base_rule_id": meta.get("base_rule_id"),
        "fixed_fee_schedule_id": meta.get("fixed_fee_schedule_id"),
        "international_surcharge_schedule_id": meta.get("international_surcharge_schedule_id"),
        "base_percentage": meta.get("base_percentage"),
        "fixed_amount": meta.get("fixed_amount"),
        "surcharge_percentage": meta.get("surcharge_percentage"),
        "predicted_total_fee": meta.get("predicted_total_fee"),
        "component_signature": meta.get("component_signature"),
        "predicted_components": components,
    }


def build_summary(run_id: str) -> dict[str, Any]:
    base = run_dir(run_id)
    plan_path = base / "plan.json"
    results_path = base / "results.json"

    summary: dict[str, Any] = {
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "planned": 0,
        "orders_created": 0,
        "buyer_approvals": 0,
        "completed_captures": 0,
        "matches": 0,
        "fee_mismatches": 0,
        "net_amount_mismatches": 0,
        "currency_mismatches": 0,
        "buyer_country_mismatches": 0,
        "library_not_calculable": 0,
        "blocked_buyer_interactions": 0,
        "api_failures": 0,
        "capability_exclusions": 0,
        "configuration_exclusions": 0,
        "pending": 0,
        "merchants_present": 0,
        "merchants_valid": 0,
        "merchants_probed": 0,
        "oauth_successful": 0,
        "oauth_failed": 0,
        "oauth_skipped": 0,
        "domestic_cases": 0,
        "distinct_schedule_cases": 0,
        "nonzero_surcharge_cases": 0,
        "no_distinct_schedule_candidates": 0,
        "no_surcharge_candidate": None,
        "dataset_revision": None,
        "payment_fee_commit": _payment_fee_commit(),
        "cases": [],
    }

    if plan_path.exists():
        plan = load_json(plan_path)
        summary["planned"] = len(plan)

    summary["dataset_revision"] = _dataset_revision()

    if not results_path.exists():
        return summary

    results = load_json(results_path)
    cases = results.get("cases", [])

    data_path = _find_data_path(cases)
    if data_path:
        summary["dataset_revision"] = _dataset_revision(data_path)

    for case in cases:
        status = case.get("status")
        rec = case.get("reconciliation", {}) or {}
        rec_status = rec.get("status")

        if case.get("order_id"):
            summary["orders_created"] += 1
        if status in {"buyer_approved", "captured", "reconciled"}:
            summary["buyer_approvals"] += 1
        if status in {"captured", "reconciled"}:
            summary["completed_captures"] += 1

        if rec_status == ReconciliationStatus.MATCH:
            summary["matches"] += 1
        elif rec_status == ReconciliationStatus.FEE_MISMATCH:
            summary["fee_mismatches"] += 1
        elif rec_status == ReconciliationStatus.NET_AMOUNT_MISMATCH:
            summary["net_amount_mismatches"] += 1
        elif rec_status == ReconciliationStatus.CURRENCY_MISMATCH:
            summary["currency_mismatches"] += 1
        elif rec_status == ReconciliationStatus.BUYER_COUNTRY_MISMATCH:
            summary["buyer_country_mismatches"] += 1
        elif rec_status in {
            ReconciliationStatus.LIBRARY_NOT_CALCULABLE,
            ReconciliationStatus.LIBRARY_MISSING_CONTEXT,
            ReconciliationStatus.LIBRARY_AMBIGUOUS,
        }:
            summary["library_not_calculable"] += 1
        elif rec_status in {
            ReconciliationStatus.BUYER_INTERACTION_BLOCKED,
            ReconciliationStatus.BUYER_CANCELLED,
            ReconciliationStatus.CALLBACK_TOKEN_MISMATCH,
        }:
            summary["blocked_buyer_interactions"] += 1
        elif rec_status in {
            ReconciliationStatus.PAYPAL_API_FAILURE,
            ReconciliationStatus.PAYPAL_FEE_UNAVAILABLE,
            ReconciliationStatus.AUTHENTICATION_FAILED,
        }:
            summary["api_failures"] += 1
        elif rec_status == ReconciliationStatus.ACCOUNT_CAPABILITY_UNAVAILABLE:
            summary["capability_exclusions"] += 1
        elif rec_status == ReconciliationStatus.ACCOUNT_CONFIGURATION_DIFFERENCE:
            summary["configuration_exclusions"] += 1
        elif rec_status == ReconciliationStatus.NO_DISTINCT_FEE_SCHEDULE_CANDIDATE:
            summary["no_distinct_schedule_candidates"] += 1
        elif status in {"planned", "prediction_ready"}:
            summary["pending"] += 1
        elif status == "failed":
            summary["api_failures"] += 1

        schedule_fields = _quote_schedule_fields(case.get("quote"))
        if case.get("pilot_metadata", {}).get("has_surcharge"):
            summary["nonzero_surcharge_cases"] += 1
        if case.get("pilot_metadata", {}).get("is_distinct_schedule"):
            summary["distinct_schedule_cases"] += 1
        if case.get("pilot_metadata", {}).get("selection_rationale") in {
            "domestic_control",
            "domestic_same_country",
        }:
            summary["domestic_cases"] += 1
        if case.get("pilot_metadata", {}).get("selection_rationale") == "no_distinct_fee_schedule_candidate":
            summary["no_distinct_schedule_candidates"] += 1

        paypal_evidence = case.get("paypal_evidence") or {}
        paypal_fee = paypal_evidence.get("paypal_fee", {}).get("value") or rec.get("paypal_fee_value")
        paypal_net = paypal_evidence.get("net_amount", {}).get("value") or rec.get("paypal_net_value")
        gross = paypal_evidence.get("gross_amount", {}).get("value") or rec.get("gross_value")
        case_summary = {
            "case_id": case.get("case_id"),
            "merchant_country": case.get("merchant_country"),
            "buyer_country": case.get("buyer_country"),
            "observed_payer_country": paypal_evidence.get("payer_country") or rec.get("observed_payer_country"),
            "amount": case.get("amount"),
            "currency": case.get("currency"),
            "gross_amount": gross,
            "paypal_fee": paypal_fee,
            "paypal_net_amount": paypal_net,
            "library_fee": rec.get("library_fee_value") or schedule_fields.get("predicted_total_fee"),
            "library_component_total": rec.get("library_fee_value") or schedule_fields.get("predicted_total_fee"),
            "status": status,
            "reconciliation_status": rec_status,
            "delta_minor_units": rec.get("delta_minor_units"),
            "paypal_fee_value": paypal_fee,
            **schedule_fields,
            "pilot_metadata": case.get("pilot_metadata", {}),
        }
        summary["cases"].append(case_summary)

    return summary


def save_summary(run_id: str, summary: dict[str, Any]) -> Path:
    path = run_dir(run_id) / "summary.json"
    save_json(path, summary)
    return path


def save_summary_markdown(run_id: str, summary: dict[str, Any], csv_path: str | None = None) -> Path:
    path = run_dir(run_id) / "summary.md"
    lines = [
        "# PayPal Sandbox Validation Summary",
        "",
        f"* Run ID: `{run_id}`",
        f"* Generated: {summary.get('generated_at', '')}",
    ]
    if csv_path:
        lines.append(f"* Account CSV: `{redact_path(csv_path)}`")
    if summary.get("dataset_revision"):
        lines.append(f"* Dataset revision: `{summary['dataset_revision']}`")
    if summary.get("payment_fee_commit"):
        lines.append(f"* payment-fee commit: `{summary['payment_fee_commit']}`")
    lines.extend(
        [
            "",
            "## Totals",
            "",
            "| Metric | Count |",
            "| --- | --- |",
            f"| Planned | {summary['planned']} |",
            f"| Orders created | {summary['orders_created']} |",
            f"| Buyer approvals | {summary['buyer_approvals']} |",
            f"| Completed captures | {summary['completed_captures']} |",
            f"| Matches | {summary['matches']} |",
            f"| Fee mismatches | {summary['fee_mismatches']} |",
            f"| Net amount mismatches | {summary['net_amount_mismatches']} |",
            f"| Currency mismatches | {summary['currency_mismatches']} |",
            f"| Buyer country mismatches | {summary['buyer_country_mismatches']} |",
            f"| Library not calculable | {summary['library_not_calculable']} |",
            f"| Blocked buyer interactions | {summary['blocked_buyer_interactions']} |",
            f"| API failures | {summary['api_failures']} |",
            f"| Capability exclusions | {summary['capability_exclusions']} |",
            f"| Configuration exclusions | {summary['configuration_exclusions']} |",
            f"| No distinct schedule candidates | {summary['no_distinct_schedule_candidates']} |",
            f"| Pending / not executed | {summary['pending']} |",
            f"| Domestic cases | {summary['domestic_cases']} |",
            f"| Distinct schedule cases | {summary['distinct_schedule_cases']} |",
            f"| Nonzero surcharge cases | {summary['nonzero_surcharge_cases']} |",
            "",
        ]
    )
    if summary.get("no_surcharge_candidate") is not None:
        lines.extend([f"| No surcharge candidate | {summary['no_surcharge_candidate']} |", ""])
    lines.extend(
        [
            "## Cases",
            "",
            (
                "| Case | Merchant | Buyer | Observed Payer | Amount | Currency | "
                "Region | Payer Region | Status | Reconciliation | Delta |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for case in summary["cases"]:
        delta = case.get("delta_minor_units")
        delta_str = str(delta) if delta is not None else ""
        lines.append(
            f"| {case.get('case_id')} | {case.get('merchant_country')} | {case.get('buyer_country')} | "
            f"{case.get('observed_payer_country') or ''} | {case.get('amount')} | {case.get('currency')} | "
            f"{case.get('transaction_region') or ''} | {case.get('payer_region') or ''} | {case.get('status')} | "
            f"{case.get('reconciliation_status')} | {delta_str} |"
        )

    completed = [c for c in summary["cases"] if c.get("status") in {"captured", "reconciled"}]
    if completed:
        lines.extend(
            [
                "",
                "## Completed Cases by Schedule",
                "",
                (
                    "| Case | Merchant | Buyer | Payer Region | Base Rule | "
                    "Fixed Schedule | Surcharge Schedule | Predicted Fee | Component Signature |"
                ),
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for case in completed:
            sig = case.get("component_signature")
            fixed = case.get("fixed_fee_schedule_id") or ""
            surcharge = case.get("international_surcharge_schedule_id") or ""
            lines.append(
                f"| {case.get('case_id')} | {case.get('merchant_country')} | {case.get('buyer_country')} | "
                f"{case.get('payer_region') or ''} | {case.get('base_rule_id') or ''} | "
                f"{fixed} | {surcharge} | "
                f"{case.get('predicted_total_fee') or ''} | {sig} |"
            )

    mismatches = [
        c
        for c in summary["cases"]
        if c.get("reconciliation_status")
        in {ReconciliationStatus.FEE_MISMATCH, ReconciliationStatus.NET_AMOUNT_MISMATCH}
    ]
    if mismatches:
        lines.extend(
            [
                "",
                "## Fee / Net Amount Mismatches",
                "",
                (
                    "| Case | Merchant | Buyer | Region | Payer Region | Currency | "
                    "Amount | PayPal Fee | Library Fee | Delta |"
                ),
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for case in mismatches:
            delta = case.get("delta_minor_units") or ""
            lines.append(
                f"| {case.get('case_id')} | {case.get('merchant_country')} | {case.get('buyer_country')} | "
                f"{case.get('transaction_region') or ''} | {case.get('payer_region') or ''} | "
                f"{case.get('currency')} | {case.get('amount')} | {case.get('paypal_fee') or ''} | "
                f"{case.get('library_fee') or ''} | {delta} |"
            )

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# JUnit status classification -------------------------------------------------

_JUNIT_FAILURES = {
    ReconciliationStatus.FEE_MISMATCH,
    ReconciliationStatus.CURRENCY_MISMATCH,
    ReconciliationStatus.NET_AMOUNT_MISMATCH,
}
_JUNIT_ERRORS = {
    ReconciliationStatus.PAYPAL_API_FAILURE,
    ReconciliationStatus.PAYPAL_FEE_UNAVAILABLE,
    ReconciliationStatus.BUYER_COUNTRY_MISMATCH,
    ReconciliationStatus.AUTHENTICATION_FAILED,
}
_JUNIT_SKIPPED = {
    ReconciliationStatus.ACCOUNT_CONFIGURATION_DIFFERENCE,
    ReconciliationStatus.ACCOUNT_CAPABILITY_UNAVAILABLE,
    ReconciliationStatus.LIBRARY_NOT_CALCULABLE,
    ReconciliationStatus.LIBRARY_MISSING_CONTEXT,
    ReconciliationStatus.LIBRARY_AMBIGUOUS,
    ReconciliationStatus.BUYER_INTERACTION_BLOCKED,
    ReconciliationStatus.BUYER_CANCELLED,
    ReconciliationStatus.CALLBACK_TOKEN_MISMATCH,
    ReconciliationStatus.NO_DISTINCT_FEE_SCHEDULE_CANDIDATE,
}


def _junit_category(rec_status: str | None, case_status: str | None) -> str | None:
    if rec_status is None and case_status == "skipped":
        return "skipped"
    if rec_status in _JUNIT_FAILURES:
        return "failure"
    if rec_status in _JUNIT_ERRORS:
        return "error"
    if rec_status in _JUNIT_SKIPPED:
        return "skipped"
    if rec_status and rec_status != ReconciliationStatus.MATCH:
        return "error"
    if case_status == "failed":
        return "error"
    return None


def save_junit(run_id: str, summary: dict[str, Any]) -> Path:
    path = run_dir(run_id) / "junit.xml"
    testsuite = ET.Element("testsuite")
    testsuite.set("name", "paypal-sandbox-validation")

    cases = summary.get("cases", [])
    total = len(cases)
    failures = 0
    errors = 0
    skipped = 0

    for case in cases:
        rec_status = case.get("reconciliation_status")
        case_status = case.get("status")
        category = _junit_category(rec_status, case_status)
        if category == "failure":
            failures += 1
        elif category == "error":
            errors += 1
        elif category == "skipped":
            skipped += 1

        testcase = ET.SubElement(testsuite, "testcase")
        testcase.set("name", f"{case['case_id']}")
        testcase.set("classname", "paypal_sandbox_validation.Case")

        if category == "failure":
            failure = ET.SubElement(testcase, "failure")
            failure.set("message", rec_status or "failure")
            failure.text = json.dumps(case, indent=2)
        elif category == "error":
            error = ET.SubElement(testcase, "error")
            error.set("message", rec_status or case_status or "error")
            error.text = json.dumps(case, indent=2)
        elif category == "skipped":
            ET.SubElement(testcase, "skipped")

    testsuite.set("tests", str(total))
    testsuite.set("failures", str(failures))
    testsuite.set("errors", str(errors))
    testsuite.set("skipped", str(skipped))

    path.write_bytes(ET.tostring(testsuite, encoding="utf-8"))
    return path
