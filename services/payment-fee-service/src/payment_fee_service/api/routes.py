from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from payment_fee import PaymentFeeEngine
from payment_fee.models import (
    CapabilityInfo,
    MarketInfo,
    ProviderInfo,
    QuoteRequest,
    QuoteResponse,
    QuoteSchema,
)

from payment_fee_service.domain.models import (
    PayPalQuoteRequest,
    StripeQuoteRequest,
)
from payment_fee_service.engine_holder import EngineHolder
from payment_fee_service.service import QuoteService
from payment_fee_service.settings import Settings

router = APIRouter()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_engine_holder(request: Request) -> EngineHolder:
    return request.app.state.engine_holder


def get_engine(request: Request) -> PaymentFeeEngine:
    return request.app.state.engine_holder.current()


def get_quote_service(request: Request) -> QuoteService:
    return QuoteService(request.app.state.engine_holder)


@router.post("/v1/quotes", response_model=QuoteResponse)
def calculate_quote_v1(
    payload: PayPalQuoteRequest | StripeQuoteRequest,
    service: Annotated[QuoteService, Depends(get_quote_service)],
) -> QuoteResponse:
    return service.calculate(payload)


@router.post("/v2/quotes", response_model=QuoteResponse)
def calculate_quote_v2(
    payload: QuoteRequest,
    engine: Annotated[PaymentFeeEngine, Depends(get_engine)],
) -> QuoteResponse:
    return engine.quote(payload)


@router.get("/v1/providers", response_model=list[ProviderInfo])
@router.get("/v2/providers", response_model=list[ProviderInfo])
def providers_v1(
    engine: Annotated[PaymentFeeEngine, Depends(get_engine)],
) -> list[ProviderInfo]:
    return engine.data_status()


@router.get("/v1/providers/{provider}/markets", response_model=list[MarketInfo])
@router.get("/v2/providers/{provider}/markets", response_model=list[MarketInfo])
def markets(
    provider: str,
    engine: Annotated[PaymentFeeEngine, Depends(get_engine)],
) -> list[MarketInfo]:
    return engine.markets(provider)


@router.get(
    "/v1/providers/{provider}/markets/{account_country}/capabilities",
    response_model=CapabilityInfo,
)
@router.get(
    "/v2/providers/{provider}/markets/{account_country}/capabilities",
    response_model=CapabilityInfo,
)
def capabilities(
    provider: str,
    account_country: str,
    engine: Annotated[PaymentFeeEngine, Depends(get_engine)],
) -> CapabilityInfo:
    return engine.capabilities(provider, account_country)


@router.get(
    "/v2/providers/{provider}/markets/{account_country}/quote-schema",
    response_model=QuoteSchema,
)
def quote_schema(
    provider: str,
    account_country: str,
    engine: Annotated[PaymentFeeEngine, Depends(get_engine)],
) -> QuoteSchema:
    return engine.quote_schema(provider, account_country)


@router.get("/v1/data/status", response_model=list[ProviderInfo])
@router.get("/v2/data/status", response_model=list[ProviderInfo])
def data_status(
    engine: Annotated[PaymentFeeEngine, Depends(get_engine)],
) -> list[ProviderInfo]:
    return engine.data_status()


@router.post("/v1/data/refresh", response_model=list[ProviderInfo])
@router.post("/v2/data/refresh", response_model=list[ProviderInfo])
async def refresh_data(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[ProviderInfo]:
    if not settings.admin_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    authorization = request.headers.get("Authorization", "")
    if authorization != f"Bearer {settings.admin_token}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    holder: EngineHolder = request.app.state.engine_holder
    return await holder.refresh(settings, raise_on_error=True)
