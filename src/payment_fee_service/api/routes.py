from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from payment_fee_service.bootstrap import refresh_registry
from payment_fee_service.domain.models import (
    CapabilityInfo,
    MarketInfo,
    ProviderInfo,
    QuoteRequest,
    QuoteResponse,
)
from payment_fee_service.providers.registry import ProviderRegistry
from payment_fee_service.service import QuoteService
from payment_fee_service.settings import Settings

router = APIRouter()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


def get_quote_service(request: Request) -> QuoteService:
    return request.app.state.quote_service


@router.post("/v1/quotes", response_model=QuoteResponse)
def calculate_quote(
    payload: QuoteRequest,
    service: Annotated[QuoteService, Depends(get_quote_service)],
) -> QuoteResponse:
    return service.calculate(payload)


@router.get("/v1/providers", response_model=list[ProviderInfo])
def providers(
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
) -> list[ProviderInfo]:
    return registry.infos()


@router.get("/v1/providers/{provider}/markets", response_model=list[MarketInfo])
def markets(
    provider: str,
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
) -> list[MarketInfo]:
    return registry.get(provider).markets()


@router.get(
    "/v1/providers/{provider}/markets/{account_country}/capabilities",
    response_model=CapabilityInfo,
)
def capabilities(
    provider: str,
    account_country: str,
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
) -> CapabilityInfo:
    return registry.get(provider).capabilities(account_country)


@router.get("/v1/data/status", response_model=list[ProviderInfo])
def data_status(
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
) -> list[ProviderInfo]:
    return registry.infos()


@router.post("/v1/data/refresh", response_model=list[ProviderInfo])
async def refresh_data(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[ProviderInfo]:
    if not settings.admin_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    authorization = request.headers.get("Authorization", "")
    if authorization != f"Bearer {settings.admin_token}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    new_registry = await refresh_registry(request.app, settings)
    return new_registry.infos()
