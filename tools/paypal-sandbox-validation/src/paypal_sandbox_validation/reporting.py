from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from paypal_sandbox_validation.persistence import load_json, run_dir, save_json
from paypal_sandbox_validation.redaction import redact_path


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
        "currency_mismatches": 0,
        "library_not_calculable": 0,
        "blocked_buyer_interactions": 0,
        "api_failures": 0,
        "capability_exclusions": 0,
        "cases": [],
    }

    if plan_path.exists():
        plan = load_json(plan_path)
        summary["planned"] = len(plan)

    if not results_path.exists():
        return summary

    results = load_json(results_path)
    cases = results.get("cases", [])
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
        elif rec_status == "currency_mismatch":
            summary["currency_mismatches"] += 1
        elif rec_status in {"library_not_calculable", "library_missing_context", "library_ambiguous"}:
            summary["library_not_calculable"] += 1
        elif rec_status == "buyer_interaction_blocked":
            summary["blocked_buyer_interactions"] += 1
        elif rec_status in {"paypal_api_failure", "paypal_fee_unavailable"}:
            summary["api_failures"] += 1
        elif rec_status in {"account_capability_unavailable", "account_configuration_difference"}:
            summary["capability_exclusions"] += 1

        summary["cases"].append(
            {
                "case_id": case.get("case_id"),
                "merchant_country": case.get("merchant_country"),
                "buyer_country": case.get("buyer_country"),
                "amount": case.get("amount"),
                "currency": case.get("currency"),
                "status": status,
                "reconciliation_status": rec_status,
                "delta_minor_units": rec.get("delta_minor_units"),
            }
        )

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
            f"| Currency mismatches | {summary['currency_mismatches']} |",
            f"| Library not calculable | {summary['library_not_calculable']} |",
            f"| Blocked buyer interactions | {summary['blocked_buyer_interactions']} |",
            f"| API failures | {summary['api_failures']} |",
            f"| Capability exclusions | {summary['capability_exclusions']} |",
            "",
            "## Cases",
            "",
            "| Case | Merchant | Buyer | Amount | Currency | Status | Reconciliation | Delta |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for case in summary["cases"]:
        delta = case.get("delta_minor_units")
        delta_str = str(delta) if delta is not None else ""
        lines.append(
            f"| {case.get('case_id')} | {case.get('merchant_country')} | {case.get('buyer_country')} | "
            f"{case.get('amount')} | {case.get('currency')} | {case.get('status')} | "
            f"{case.get('reconciliation_status')} | {delta_str} |"
        )

    # Mismatch grouping
    mismatches = [c for c in summary["cases"] if c.get("reconciliation_status") == "fee_mismatch"]
    if mismatches:
        lines.extend(
            [
                "",
                "## Fee Mismatches",
                "",
                "| Case | Merchant | Buyer | Currency | Amount | Delta |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for case in mismatches:
            delta = case.get("delta_minor_units") or ""
            lines.append(
                f"| {case.get('case_id')} | {case.get('merchant_country')} | {case.get('buyer_country')} | "
                f"{case.get('currency')} | {case.get('amount')} | {delta} |"
            )

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def save_junit(run_id: str, summary: dict[str, Any]) -> Path:
    path = run_dir(run_id) / "junit.xml"
    testsuite = ET.Element("testsuite")
    testsuite.set("name", "paypal-sandbox-validation")
    testsuite.set("tests", str(summary["planned"]))
    testsuite.set("failures", str(summary["fee_mismatches"] + summary["currency_mismatches"]))
    testsuite.set("errors", str(summary["api_failures"]))
    testsuite.set("skipped", str(summary["capability_exclusions"] + summary["library_not_calculable"]))

    for case in summary["cases"]:
        testcase = ET.SubElement(testsuite, "testcase")
        testcase.set("name", f"{case['case_id']}")
        testcase.set("classname", "paypal_sandbox_validation.Case")
        rec_status = case.get("reconciliation_status")
        if rec_status and rec_status != "match":
            failure = ET.SubElement(testcase, "failure")
            failure.set("message", rec_status)
            failure.text = json.dumps(case, indent=2)
        elif case.get("status") == "skipped":
            ET.SubElement(testcase, "skipped")

    path.write_bytes(ET.tostring(testsuite, encoding="utf-8"))
    return path
