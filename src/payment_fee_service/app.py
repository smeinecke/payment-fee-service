from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from payment_fee_service import __version__
from payment_fee_service.api.routes import router
from payment_fee_service.bootstrap import refresh_registry
from payment_fee_service.domain.errors import ServiceError
from payment_fee_service.providers.registry import ProviderRegistry
from payment_fee_service.service import QuoteService
from payment_fee_service.settings import Settings


def create_app(
    settings: Settings | None = None,
    registry: ProviderRegistry | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = resolved_settings
        if registry is None:
            await refresh_registry(app, resolved_settings)
        else:
            app.state.registry = registry
            app.state.quote_service = QuoteService(registry)

        refresh_task: asyncio.Task[None] | None = None
        if resolved_settings.refresh_interval_seconds > 0:

            async def _refresh_loop() -> None:
                while True:
                    await asyncio.sleep(resolved_settings.refresh_interval_seconds)
                    await refresh_registry(app, resolved_settings)

            refresh_task = asyncio.create_task(_refresh_loop())

        try:
            yield
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with suppress(asyncio.CancelledError):
                    await refresh_task

    app = FastAPI(
        title="Payment Fee Service",
        version=__version__,
        description=(
            "Estimate public standard transaction fees using versioned PayPal and Stripe "
            "fee datasets. "
            "Results are estimates and must not be used as authoritative billing statements."
        ),
        openapi_url="/docs/openapi.json",
        lifespan=lifespan,
    )
    app.include_router(router)

    @app.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness(request: Request) -> JSONResponse:
        ready = request.app.state.registry.ready
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready"},
        )

    return app


app = create_app()
