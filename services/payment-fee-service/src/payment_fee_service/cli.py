from __future__ import annotations

import argparse
import json
import os

import uvicorn

from payment_fee_service.bootstrap import build_engine
from payment_fee_service.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="payment-fee-service")
    parser.add_argument("--config-file", help="Path to a JSON configuration file")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Run the HTTP service")
    serve.add_argument("--host", default="0.0.0.0")  # nosec: B104
    serve.add_argument("--port", type=int, default=8000)
    subparsers.add_parser("validate-data", help="Load and validate configured provider datasets")
    args = parser.parse_args()

    if args.config_file:
        os.environ["PAYMENT_FEE_CONFIG_FILE"] = args.config_file

    if args.command == "serve":
        uvicorn.run("payment_fee_service.app:app", host=args.host, port=args.port)
        return

    engine = build_engine(Settings(fail_startup_on_data_error=True))
    print(json.dumps([info.model_dump() for info in engine.data_status()], indent=2))
