from __future__ import annotations

from typing import Any

from payment_fee import PaymentFeeEngine
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
from payment_fee.models import (
    CardContext as LibCardContext,
)
from payment_fee.models import (
    Money as LibMoney,
)
from payment_fee.models import (
    PayPalQuoteRequest as LibPayPalQuoteRequest,
)
from payment_fee.models import (
    PayPalTransaction as LibPayPalTransaction,
)
from payment_fee.models import (
    QuoteResponse,
)
from payment_fee.models import (
    StripeQuoteRequest as LibStripeQuoteRequest,
)
from payment_fee.models import (
    StripeTransaction as LibStripeTransaction,
)

from payment_fee_service.domain.errors import (
    AmbiguousRulesError,
    DataUnavailableError,
    InsufficientContextError,
    QuoteUnavailableError,
    UnknownMarketError,
    UnknownProviderError,
)
from payment_fee_service.domain.models import (
    PayPalQuoteRequest,
    StripeQuoteRequest,
)

_PAYPAL_PRODUCT_MAP: dict[str, str] = {
    "standard_commercial": "other_commercial",
    "goods_and_services": "goods_and_services",
    "micropayments": "micropayments",
    "donations": "donations",
    "nonprofit": "nonprofit",
}


def _to_service_error(exc: PaymentFeeError) -> Exception:
    if isinstance(exc, UnknownProvider):
        return UnknownProviderError(exc.details.get("provider", "unknown"))
    if isinstance(exc, UnknownMarket):
        return UnknownMarketError(
            exc.details.get("provider", "unknown"),
            exc.details.get("market", "unknown"),
        )
    if isinstance(exc, ProviderDataUnavailable):
        return DataUnavailableError(
            exc.details.get("provider", "unknown"),
            exc.details.get("reason", "unknown"),
        )
    if isinstance(exc, InsufficientTransactionContext):
        return InsufficientContextError(
            exc.details.get("missing_fields", []),
            **{k: v for k, v in exc.details.items() if k != "missing_fields"},
        )
    if isinstance(exc, AmbiguousFeeRules):
        return AmbiguousRulesError(exc.details.get("candidate_rule_ids", []))
    if isinstance(exc, (QuoteNotAvailable, CurrencyMismatch, UnsupportedFeeShape, DatasetValidationError)):
        return QuoteUnavailableError(exc.message, **exc.details)
    return QuoteUnavailableError(exc.message, **exc.details)


class QuoteService:
    def __init__(self, engine: PaymentFeeEngine) -> None:
        self._engine = engine

    def calculate(self, request: PayPalQuoteRequest | StripeQuoteRequest) -> QuoteResponse:
        try:
            if isinstance(request, PayPalQuoteRequest):
                lib_request = _convert_paypal(request)
            elif isinstance(request, StripeQuoteRequest):
                lib_request = _convert_stripe(request)
            else:
                raise UnknownProviderError(request.provider)
            return self._engine.quote(lib_request)
        except PaymentFeeError as exc:
            raise _to_service_error(exc) from exc


def _convert_paypal(request: PayPalQuoteRequest) -> LibPayPalQuoteRequest:
    payment = request.payment
    account_country = request.account_country.upper()
    customer_country = (request.customer_country or account_country).upper()
    transaction_region = "domestic" if account_country == customer_country else "international"
    product_id = _PAYPAL_PRODUCT_MAP.get(payment.transaction_type, payment.transaction_type)
    return LibPayPalQuoteRequest(
        provider="paypal",
        amount=LibMoney.model_validate(request.amount.model_dump()),
        account_country=account_country,
        customer_country=customer_country,
        settlement_currency=request.settlement_currency,
        transaction=LibPayPalTransaction(
            product_id=product_id,
            variant_id="standard",
            payment_method=None,
            transaction_region=transaction_region,
            surcharge_region=payment.surcharge_region,
            payer_region=payment.surcharge_region,
        ),
    )


def _convert_stripe(request: StripeQuoteRequest) -> LibStripeQuoteRequest:
    payment = request.payment
    account_country = request.account_country.upper()
    customer_country = (request.customer_country or account_country).upper()
    is_domestic = account_country == customer_country
    card = payment.card

    card_origin = card.origin.lower() if card and card.origin else "domestic" if is_domestic else "international"

    if card and card.region:
        card_region = _normalize_card_region(card.region, card_origin)
    elif is_domestic:
        card_region = "domestic"
    else:
        card_region = None

    card_tier = card.tier if card and card.tier else "standard"
    product_id, variant_id = _stripe_product_variant(payment)

    context: dict[str, Any] = dict(payment.context)
    context.setdefault("transaction_type", "charge")

    transaction = LibStripeTransaction(
        product_id=product_id,
        variant_id=variant_id,
        payment_method=payment.method,
        channel=payment.channel,
        recurring=payment.recurring,
        billing_type=payment.billing_type,
        currency_conversion_required=payment.currency_conversion_required,
        card=LibCardContext(
            origin=card_origin,
            region=card_region,
            tier=card_tier,
        )
        if card
        else None,
        context=context,
    )
    if product_id == "payments" and payment.method == "card":
        transaction.pricing_tier = "standard"

    return LibStripeQuoteRequest(
        provider="stripe",
        amount=LibMoney.model_validate(request.amount.model_dump()),
        account_country=account_country,
        customer_country=customer_country,
        settlement_currency=request.settlement_currency,
        transaction=transaction,
    )


def _normalize_card_region(region: str, origin: str) -> str:
    region = region.lower()
    domestic_aliases = {"domestic", "eea", "europe", "european_economic_area"}
    international_aliases = {"international", "non-eea", "rest_of_world", "global"}
    if region in domestic_aliases:
        return "domestic"
    if region in international_aliases:
        return "international"
    return "domestic" if origin == "domestic" else "international"


def _stripe_product_variant(payment: Any) -> tuple[str, str]:
    if payment.method == "card":
        if payment.channel == "in_person":
            return "terminal", "domestic_cards"
        return "payments", "online_domestic_cards"
    if payment.method in {"sepa_debit", "sepa_direct_debit"}:
        return "sepa_direct_debit", "standard"
    if payment.method in {"sepa_bank_transfer", "bank_transfer"}:
        return "sepa_bank_transfer", "standard"
    return payment.method, "standard"
