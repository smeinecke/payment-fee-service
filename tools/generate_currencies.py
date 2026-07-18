#!/usr/bin/env python3
"""Generate contracts/currencies.json from an ISO 4217 source.

The canonical source is JupiterInvoice's currency-codes.json by default.
Override with --source-url or --source-file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_TARGET = Path(__file__).resolve().parents[1] / "contracts" / "currencies.json"
DEFAULT_SOURCE_URL = "https://jupiterinvoice.com/reference/currency-codes.json"


def load_source(path_or_url: str) -> dict:
    if path_or_url.startswith(("http://", "https://")):
        import urllib.request

        with urllib.request.urlopen(path_or_url) as response:
            return json.loads(response.read().decode("utf-8"))
    return json.loads(Path(path_or_url).read_text(encoding="utf-8"))


def build_currencies(data: dict) -> dict:
    currencies: dict[str, dict[str, int]] = {}
    for row in data.get("rows", []):
        code = row.get("code")
        if not code:
            continue
        minor = row.get("decimal_places")
        if minor is None:
            minor = 2
        currencies[code] = {"minor_units": minor}
    return dict(sorted(currencies.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate currency metadata.")
    parser.add_argument(
        "--source-url",
        default=DEFAULT_SOURCE_URL,
        help="URL or local path to the ISO 4217 source JSON.",
    )
    parser.add_argument(
        "--source-file",
        default=None,
        help="Local file to use instead of --source-url.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with error if the target file would change.",
    )
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="Output file path.")
    args = parser.parse_args()

    source = args.source_file or args.source_url
    data = load_source(source)
    currencies = build_currencies(data)
    output = json.dumps(currencies, indent=2, sort_keys=True) + "\n"

    target = Path(args.target)
    if args.check:
        if not target.exists():
            print(f"Missing {target}", file=sys.stderr)
            return 1
        existing = target.read_text(encoding="utf-8")
        if existing != output:
            print(f"{target} is out of date. Run: python {Path(__file__).name}", file=sys.stderr)
            return 1
        print(f"{target} is up to date.")
        return 0

    target.write_text(output, encoding="utf-8")
    print(f"Wrote {len(currencies)} currencies to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
