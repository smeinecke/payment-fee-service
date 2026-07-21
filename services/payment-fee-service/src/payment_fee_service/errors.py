from __future__ import annotations

from payment_fee.errors import (
    AmbiguousFeeRules,
    CurrencyMismatch,
    DatasetValidationError,
    InsufficientTransactionContext,
    PaymentFeeError,
    ProviderDataUnavailable,
    QuoteNotAvailable,
    UnknownMarket,
    UnknownProvider,
    UnsupportedFeeShape,
)


def error_message(exc: BaseException) -> str:
    """Return the public error message for any exception."""
    if isinstance(exc, PaymentFeeError):
        return exc.message
    return str(exc)


def status_for(exc: PaymentFeeError) -> int:
    """Map a PaymentFeeError to the appropriate HTTP status code."""
    if isinstance(exc, (UnknownProvider, UnknownMarket)):
        return 404
    if isinstance(exc, (InsufficientTransactionContext, CurrencyMismatch, QuoteNotAvailable, UnsupportedFeeShape)):
        return 422
    if isinstance(exc, AmbiguousFeeRules):
        return 409
    if isinstance(exc, (DatasetValidationError, ProviderDataUnavailable)):
        return 503
    return 500
