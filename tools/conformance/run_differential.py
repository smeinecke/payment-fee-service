#!/usr/bin/env python3
"""Cross-language conformance differential runner.

Runs the Python, TypeScript and PHP conformance suites, captures their actual
results, and compares them case-by-case. The suites already compare each
implementation against the expected fixtures; this runner adds an extra layer
that fails if any implementation disagrees with the others.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def strip_nulls(value: object) -> object:
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


def normalize(value: object) -> object:
    if isinstance(value, dict):
        return tuple((k, normalize(v)) for k, v in sorted(value.items()))
    if isinstance(value, list):
        return tuple(normalize(v) for v in value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


def run_implementation(name: str, cmd: list[str], emit_path: Path) -> list[dict]:
    result = subprocess.run(cmd + ["--emit", str(emit_path)], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"{name} suite failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return json.loads(emit_path.read_text())


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        py_path = tmp / "python.json"
        ts_path = tmp / "typescript.json"
        php_path = tmp / "php.json"

        py_results = run_implementation("Python", ["uv", "run", "python", "run_python.py"], py_path)
        ts_results = run_implementation("TypeScript", ["node", "run_typescript.mjs"], ts_path)
        php_results = run_implementation("PHP", ["php", "run_php.php"], php_path)

    by_id = {
        "python": {r["id"]: r for r in py_results},
        "typescript": {r["id"]: r for r in ts_results},
        "php": {r["id"]: r for r in php_results},
    }

    ids = sorted(by_id["python"].keys())
    mismatches: list[dict] = []
    for case_id in ids:
        py_key = normalize(
            strip_nulls(
                {"actual": by_id["python"][case_id].get("actual"), "error": by_id["python"][case_id].get("error")}
            )
        )
        ts = by_id["typescript"].get(case_id)
        php = by_id["php"].get(case_id)
        ts_key = normalize(
            strip_nulls({"actual": ts.get("actual") if ts else None, "error": ts.get("error") if ts else None})
        )
        php_key = normalize(
            strip_nulls({"actual": php.get("actual") if php else None, "error": php.get("error") if php else None})
        )

        if py_key != ts_key or py_key != php_key:
            mismatches.append(
                {
                    "id": case_id,
                    "python": by_id["python"][case_id],
                    "typescript": by_id["typescript"].get(case_id),
                    "php": by_id["php"].get(case_id),
                }
            )

    if mismatches:
        print(f"\nCross-language mismatches: {len(mismatches)}", file=sys.stderr)
        for mismatch in mismatches:
            print(json.dumps(mismatch, indent=2, default=str), file=sys.stderr)
        return 1

    print("\nAll cross-language differential checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
