from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from payment_fee import PaymentFeeEngine
from payment_fee.errors import PaymentFeeError, ProviderDataUnavailable

from payment_fee_service import __version__
from payment_fee_service.api.routes import router
from payment_fee_service.engine_holder import EngineHolder
from payment_fee_service.errors import error_message, status_for
from payment_fee_service.settings import Settings

logger = logging.getLogger(__name__)


def _error_response(exc: PaymentFeeError, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": exc.code,
                "message": error_message(exc),
                "details": exc.details,
            }
        },
    )


def create_app(
    settings: Settings | None = None,
    engine: PaymentFeeEngine | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = resolved_settings
        app.state.engine_holder = EngineHolder(engine)

        if engine is None:
            try:
                await app.state.engine_holder.refresh(resolved_settings)
            except Exception as exc:
                logger.warning("Startup engine build failed: %s", exc)

        refresh_task: asyncio.Task[None] | None = None
        if resolved_settings.refresh_interval_seconds > 0:

            async def _refresh_loop() -> None:
                while True:
                    await asyncio.sleep(resolved_settings.refresh_interval_seconds)
                    try:
                        await app.state.engine_holder.refresh(resolved_settings)
                    except Exception:
                        logger.exception("Background refresh failed.")

            refresh_task = asyncio.create_task(_refresh_loop())

        try:
            yield
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
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

    @app.exception_handler(PaymentFeeError)
    async def payment_fee_error_handler(_: Request, exc: PaymentFeeError) -> JSONResponse:
        return _error_response(exc, status_for(exc))

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness(request: Request) -> JSONResponse:
        try:
            request.app.state.engine_holder.current()
            ready = True
        except ProviderDataUnavailable:
            ready = False
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready"},
        )

    return app


app = create_app()
