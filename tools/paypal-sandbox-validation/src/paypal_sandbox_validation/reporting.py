from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    matched_rules = quote.get("matched_rules", []) or []
    base_rule_id = None
    surcharge_schedule_id = None
    if matched_rules:
        base_rule_id = matched_rules[0].get("rule_id") if isinstance(matched_rules[0], dict) else matched_rules[0]
    if len(matched_rules) > 1:
        surcharge_schedule_id = (
            matched_rules[1].get("rule_id") if isinstance(matched_rules[1], dict) else matched_rules[1]
        )
    components = quote.get("components", []) or []
    return {
        "transaction_region": transaction.get("transaction_region"),
        "payer_region": transaction.get("payer_region"),
        "product_id": quote.get("_scenario", {}).get("product_id"),
        "variant_id": quote.get("_scenario", {}).get("variant_id"),
        "base_rule_id": base_rule_id,
        "international_surcharge_schedule_id": surcharge_schedule_id,
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

        if rec_status == "match":
            summary["matches"] += 1
        elif rec_status == "fee_mismatch":
            summary["fee_mismatches"] += 1
        elif rec_status == "net_amount_mismatch":
            summary["net_amount_mismatches"] += 1
        elif rec_status == "currency_mismatch":
            summary["currency_mismatches"] += 1
        elif rec_status == "buyer_country_mismatch":
            summary["buyer_country_mismatches"] += 1
        elif rec_status in {"library_not_calculable", "library_missing_context", "library_ambiguous"}:
            summary["library_not_calculable"] += 1
        elif rec_status in {"buyer_interaction_blocked", "buyer_cancelled", "callback_token_mismatch"}:
            summary["blocked_buyer_interactions"] += 1
        elif rec_status in {"paypal_api_failure", "paypal_fee_unavailable", "authentication_failed"}:
            summary["api_failures"] += 1
        elif rec_status == "account_capability_unavailable":
            summary["capability_exclusions"] += 1
        elif rec_status == "account_configuration_difference":
            summary["configuration_exclusions"] += 1
        elif status in {"planned", "prediction_ready"}:
            summary["pending"] += 1
        elif status == "failed":
            summary["api_failures"] += 1

        schedule_fields = _quote_schedule_fields(case.get("quote"))
        case_summary = {
            "case_id": case.get("case_id"),
            "merchant_country": case.get("merchant_country"),
            "buyer_country": case.get("buyer_country"),
            "observed_payer_country": (case.get("paypal_evidence") or {}).get("payer_country")
            or rec.get("observed_payer_country"),
            "amount": case.get("amount"),
            "currency": case.get("currency"),
            "status": status,
            "reconciliation_status": rec_status,
            "delta_minor_units": rec.get("delta_minor_units"),
            "paypal_fee": (case.get("paypal_evidence") or {}).get("paypal_fee", {}).get("value")
            or rec.get("paypal_fee_value"),
            "library_fee": rec.get("library_fee_value"),
            **schedule_fields,
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
            f"| Pending / not executed | {summary['pending']} |",
            "",
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

    mismatches = [
        c for c in summary["cases"] if c.get("reconciliation_status") in {"fee_mismatch", "net_amount_mismatch"}
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

_JUNIT_FAILURES = {"fee_mismatch", "currency_mismatch", "net_amount_mismatch"}
_JUNIT_ERRORS = {"paypal_api_failure", "paypal_fee_unavailable", "buyer_country_mismatch", "authentication_failed"}
_JUNIT_SKIPPED = {
    "account_configuration_difference",
    "account_capability_unavailable",
    "library_not_calculable",
    "library_missing_context",
    "library_ambiguous",
    "buyer_interaction_blocked",
    "buyer_cancelled",
    "callback_token_mismatch",
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
    if rec_status and rec_status != "match":
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
