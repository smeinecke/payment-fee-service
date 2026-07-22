from __future__ import annotations

import os

import click


def _env_csv_default() -> str | None:
    return os.environ.get("PAYPAL_SANDBOX_ACCOUNTS_CSV")


def _cli() -> None:
    """PayPal Sandbox fee-reconciliation harness."""


cli = click.group()(_cli)
cli = click.version_option(version="0.1.0")(cli)

# isort: off
from . import diagnose as diagnose  # noqa: E402
from . import execution as execution  # noqa: E402
from . import manual as manual  # noqa: E402
from . import manual_approval as manual_approval  # noqa: E402
from . import probing as probing  # noqa: E402
from . import profile_pricing as profile_pricing  # noqa: E402
from . import qualify as qualify  # noqa: E402
from . import reconcile_report as reconcile_report  # noqa: E402
from . import runner as runner  # noqa: E402
from . import verify as verify  # noqa: E402
from .manual import _manual_consistency_checks as _manual_consistency_checks  # noqa: E402
from .qualify import (  # noqa: E402
    _attempt_public_rate_reuse as _attempt_public_rate_reuse,
    _filter_representative_merchants as _filter_representative_merchants,
    _parse_requested_merchants as _parse_requested_merchants,
    _select_target_merchants as _select_target_merchants,
)
# isort: on


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
