from __future__ import annotations

import asyncio

from fastapi import FastAPI
from payment_fee import PaymentFeeEngine

from payment_fee_service.data.source import DataLocation, JsonDataSource
from payment_fee_service.settings import Settings


def _load_provider_documents(source: JsonDataSource, validate: bool, provider: str) -> dict[str, dict]:
    core = source.read_json("json/core-fees.json")
    index = source.read_json("json/index.json")
    if validate:
        source.validate(core, "schemas/core-fees-v1.schema.json")
        source.validate(index, "schemas/index-v1.schema.json")
    docs: dict[str, dict] = {"core": core, "index": index}
    if provider == "stripe":
        try:
            payment_methods = source.read_json("json/payment-methods.json")
            if validate:
                source.validate(payment_methods, "schemas/payment-methods-v1.schema.json")
            docs["payment_methods"] = payment_methods
        except FileNotFoundError:
            pass
    return docs


def build_engine(settings: Settings, fail_on_error: bool | None = None) -> PaymentFeeEngine:
    if fail_on_error is None:
        fail_on_error = settings.fail_startup_on_data_error
    paypal_docs: dict[str, dict] | None = None
    stripe_docs: dict[str, dict] | None = None
    errors: list[tuple[str, Exception]] = []
    for provider_id, provider_settings in settings.providers.items():
        if not provider_settings.enabled:
            continue
        try:
            source = JsonDataSource(
                DataLocation(
                    provider=provider_id,
                    local_path=provider_settings.data_path,
                    base_url=provider_settings.data_url,
                    data_ref=provider_settings.data_ref,
                ),
                settings.http_timeout_seconds,
            )
            docs = _load_provider_documents(source, settings.validate_json_schema, provider_id)
            if provider_id == "paypal":
                paypal_docs = docs
            elif provider_id == "stripe":
                stripe_docs = docs
            else:
                raise ValueError(f"Unsupported provider: {provider_id}")
        except Exception as exc:
            if fail_on_error:
                raise
            errors.append((provider_id, exc))

    engine = PaymentFeeEngine.from_documents(
        paypal=paypal_docs,
        stripe=stripe_docs,
    )
    for provider_id, exc in errors:
        engine._registry.register_error(provider_id, exc)
    return engine


async def refresh_engine(app: FastAPI, settings: Settings) -> PaymentFeeEngine:
    engine = await asyncio.to_thread(build_engine, settings, False)
    app.state.engine = engine
    return engine
