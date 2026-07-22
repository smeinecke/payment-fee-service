from __future__ import annotations

import json

import click

from paypal_sandbox_validation.persistence import (
    artifact_root,
    load_results,
    save_results,
)
from paypal_sandbox_validation.reconciliation import reconcile
from paypal_sandbox_validation.reporting import build_summary, save_junit, save_summary, save_summary_markdown

from . import _env_csv_default, cli


@cli.command("reconcile")
@click.option("--run-id", type=str, required=True)
def reconcile_cmd(run_id: str) -> None:
    """Re-run reconciliation for a captured run without creating new orders."""
    cases = []
    for case in load_results(run_id).get("cases", []):
        if case.get("status") == "captured" and case.get("paypal_evidence") and case.get("quote"):
            rec = reconcile(
                case["paypal_evidence"],
                case["quote"],
                case["merchant_country"],
                case["buyer_country"],
                case["paypal_evidence"].get("payer_country"),
            )
            case["reconciliation"] = rec.model_dump()
            case["status"] = "reconciled"
        cases.append(case)
    save_results(run_id, {"run_id": run_id, "cases": cases})
    summary = build_summary(run_id)
    save_summary(run_id, summary)
    save_summary_markdown(run_id, summary)
    save_junit(run_id, summary)
    click.echo(json.dumps({k: v for k, v in summary.items() if k != "cases"}, indent=2))


@cli.command("report")
@click.option("--run-id", type=str, required=True)
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
def report_cmd(run_id: str, accounts_csv: str) -> None:
    """Generate a sanitized report for a run."""
    summary = build_summary(run_id)
    save_summary(run_id, summary)
    save_summary_markdown(run_id, summary, accounts_csv)
    save_junit(run_id, summary)

    click.echo("Sanitized report generated.")
    click.echo(f"Artifact path: {artifact_root() / run_id}")
    click.echo(json.dumps({k: v for k, v in summary.items() if k != "cases"}, indent=2))
