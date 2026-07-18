from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from payment_fee import PaymentFeeEngine
from payment_fee.errors import PaymentFeeError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="payment-fee", description="Payment fee calculation utility.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    providers_parser = subparsers.add_parser("providers", help="List configured providers.")
    providers_parser.add_argument("--paypal", type=Path)
    providers_parser.add_argument("--stripe", type=Path)
    providers_parser.add_argument("--validate", action="store_true")

    quote_parser = subparsers.add_parser("quote", help="Compute a fee quote from JSON request.")
    quote_parser.add_argument("--paypal", type=Path)
    quote_parser.add_argument("--stripe", type=Path)
    quote_parser.add_argument("--validate", action="store_true")
    quote_parser.add_argument("request", help="JSON request object or path to JSON file.")

    validate_parser = subparsers.add_parser("validate-data", help="Validate provider datasets.")
    validate_parser.add_argument("--paypal", type=Path, required=True)
    validate_parser.add_argument("--stripe", type=Path, required=True)

    args = parser.parse_args(argv)

    try:
        if args.command == "providers":
            return _providers(args)
        if args.command == "quote":
            return _quote(args)
        if args.command == "validate-data":
            return _validate(args)
    except PaymentFeeError as exc:
        sys.stderr.write(f"{exc.code}: {exc.message}\n")
        return 1

    return 0


def _engine(args: argparse.Namespace) -> PaymentFeeEngine:
    return PaymentFeeEngine.from_paths(
        paypal=str(args.paypal) if args.paypal else None,
        stripe=str(args.stripe) if args.stripe else None,
        validate=args.validate,
    )


def _providers(args: argparse.Namespace) -> int:
    if not args.paypal and not args.stripe:
        sys.stderr.write("Provide --paypal and/or --stripe data paths.\n")
        return 2
    engine = _engine(args)
    print(json.dumps(engine.providers()))
    return 0


def _quote(args: argparse.Namespace) -> int:
    if not args.paypal and not args.stripe:
        sys.stderr.write("Provide --paypal and/or --stripe data paths.\n")
        return 2
    raw = args.request
    payload = json.loads(Path(raw).read_bytes()) if Path(raw).is_file() else json.loads(raw)
    engine = _engine(args)
    response = engine.quote(payload)
    print(response.model_dump_json(indent=2))
    return 0


def _validate(args: argparse.Namespace) -> int:
    engine = PaymentFeeEngine.from_paths(
        paypal=str(args.paypal),
        stripe=str(args.stripe),
        validate=True,
    )
    print(json.dumps([info.model_dump() for info in engine.data_status()], indent=2, default=_json_default))
    return 0


def _json_default(value: Any) -> Any:
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
