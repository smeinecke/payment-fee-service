from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paypal_sandbox_validation.models import Case
from paypal_sandbox_validation.redaction import sanitize_paypal_capture, sanitize_paypal_order


def artifact_root() -> Path:
    root = Path("artifacts/paypal-sandbox")
    root.mkdir(parents=True, exist_ok=True)
    return root


def manual_artifact_root() -> Path:
    root = Path("artifacts/paypal-sandbox-manual")
    root.mkdir(parents=True, exist_ok=True)
    return root


def manual_run_dir(run_id: str) -> Path:
    path = manual_artifact_root() / run_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "cases").mkdir(exist_ok=True)
    (path / "sanitized-ui").mkdir(exist_ok=True)
    (path / "screenshots").mkdir(exist_ok=True)
    return path


def run_dir(run_id: str) -> Path:
    path = artifact_root() / run_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "cases").mkdir(exist_ok=True)
    (path / "sanitized-api").mkdir(exist_ok=True)
    (path / "screenshots").mkdir(exist_ok=True)
    return path


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, default=str)


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_plan(run_id: str, plan: list[Case]) -> Path:
    path = run_dir(run_id) / "plan.json"
    save_json(path, [c.model_dump() for c in plan])
    return path


def load_plan(run_id: str) -> list[Case]:
    path = run_dir(run_id) / "plan.json"
    data = load_json(path)
    return [Case.model_validate(item) for item in data]


def save_case(run_id: str, case: Case) -> Path:
    path = run_dir(run_id) / "cases" / f"{case.case_id}.json"
    save_json(path, case.model_dump())
    return path


def load_case(run_id: str, case_id: str) -> Case:
    path = run_dir(run_id) / "cases" / f"{case_id}.json"
    return Case.model_validate(load_json(path))


def list_cases(run_id: str) -> list[Case]:
    cases_dir = run_dir(run_id) / "cases"
    cases = []
    for file in sorted(cases_dir.glob("*.json")):
        cases.append(Case.model_validate(load_json(file)))
    return cases


def save_configuration_summary(run_id: str, summary: dict[str, Any]) -> Path:
    path = run_dir(run_id) / "configuration-summary.json"
    save_json(path, summary)
    return path


def save_oauth_probe_summary(run_id: str, probes: list[dict[str, Any]]) -> Path:
    path = run_dir(run_id) / "oauth-probe-summary.json"
    save_json(path, probes)
    return path


def save_results(run_id: str, results: dict[str, Any]) -> Path:
    path = run_dir(run_id) / "results.json"
    save_json(path, results)
    return path


def load_results(run_id: str) -> dict[str, Any]:
    path = run_dir(run_id) / "results.json"
    if path.exists():
        return load_json(path)
    return {}


def save_sanitized_order(run_id: str, case_id: str, order: dict[str, Any]) -> Path:
    path = run_dir(run_id) / "sanitized-api" / f"{case_id}-order.json"
    save_json(path, sanitize_paypal_order(order))
    return path


def save_sanitized_capture(run_id: str, case_id: str, capture: dict[str, Any]) -> Path:
    path = run_dir(run_id) / "sanitized-api" / f"{case_id}-capture.json"
    save_json(path, sanitize_paypal_capture(capture))
    return path


def list_run_ids() -> list[str]:
    root = artifact_root()
    if not root.exists():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir())


def save_manual_plan(run_id: str, plan: list[Case]) -> Path:
    path = manual_run_dir(run_id) / "plan.json"
    save_json(path, [c.model_dump() for c in plan])
    return path


def load_manual_plan(run_id: str) -> list[Case]:
    path = manual_run_dir(run_id) / "plan.json"
    data = load_json(path)
    return [Case.model_validate(item) for item in data]


def save_manual_case(run_id: str, case: Case) -> Path:
    path = manual_run_dir(run_id) / "cases" / f"{case.case_id}.json"
    save_json(path, case.model_dump())
    return path


def load_manual_case(run_id: str, case_id: str) -> Case:
    path = manual_run_dir(run_id) / "cases" / f"{case_id}.json"
    return Case.model_validate(load_json(path))


def list_manual_cases(run_id: str) -> list[Case]:
    cases_dir = manual_run_dir(run_id) / "cases"
    cases = []
    for file in sorted(cases_dir.glob("*.json")):
        cases.append(Case.model_validate(load_json(file)))
    return cases


def save_manual_results(run_id: str, results: dict[str, Any]) -> Path:
    path = manual_run_dir(run_id) / "results.json"
    save_json(path, results)
    return path


def load_manual_results(run_id: str) -> dict[str, Any]:
    path = manual_run_dir(run_id) / "results.json"
    if path.exists():
        return load_json(path)
    return {}


def _private_state_path(run_id: str) -> Path:
    return manual_run_dir(run_id) / "private" / "nvp-private.json"


def save_manual_private_state(run_id: str, case_id: str, transaction_id: str) -> Path:
    path = _private_state_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_manual_private_state(run_id)
    data[case_id] = transaction_id
    save_json(path, data)
    return path


def load_manual_private_state(run_id: str) -> dict[str, str]:
    path = _private_state_path(run_id)
    if path.exists():
        return load_json(path)
    return {}
