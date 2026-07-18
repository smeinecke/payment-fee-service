"""Payment fee calculation library."""

from payment_fee.engine import PaymentFeeEngine
from payment_fee.errors import (
    AmbiguousFeeRules,
    CurrencyMismatch,
    DatasetValidationError,
    InsufficientTransactionContext,
    ProviderDataUnavailable,
    QuoteNotAvailable,
    UnknownMarket,
    UnknownProvider,
    UnsupportedFeeShape,
)
from payment_fee.models import (
    CapabilityInfo,
    DataProvenance,
    FeeComponent,
    MarketInfo,
    Money,
    PayPalQuoteRequest,
    PayPalTransaction,
    ProviderInfo,
    QuoteRequest,
    QuoteResponse,
    QuoteSchema,
    StripeQuoteRequest,
    StripeTransaction,
)

__all__ = [
    "AmbiguousFeeRules",
    "CapabilityInfo",
    "CurrencyMismatch",
    "DataProvenance",
    "DatasetValidationError",
    "FeeComponent",
    "InsufficientTransactionContext",
    "MarketInfo",
    "Money",
    "PayPalQuoteRequest",
    "PayPalTransaction",
    "PaymentFeeEngine",
    "ProviderDataUnavailable",
    "ProviderInfo",
    "QuoteNotAvailable",
    "QuoteRequest",
    "QuoteResponse",
    "QuoteSchema",
    "StripeQuoteRequest",
    "StripeTransaction",
    "UnknownMarket",
    "UnknownProvider",
    "UnsupportedFeeShape",
]

__version__ = "0.4.0"
