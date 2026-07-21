from __future__ import annotations

from payment_fee import PaymentFeeEngine
from payment_fee.errors import PaymentFeeError, ProviderDataUnavailable

from payment_fee_service.data.source import DataLocation, JsonDataSource
from payment_fee_service.settings import Settings


def _load_provider_documents(source: JsonDataSource, validate: bool) -> dict[str, dict]:
    core = source.read_json("json/core-fees.json")
    index = source.read_json("json/index.json")
    if validate:
        source.validate(core, "schemas/core-fees-v1.schema.json")
        source.validate(index, "schemas/index-v1.schema.json")
    return {"core": core, "index": index}


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
            docs = _load_provider_documents(source, settings.validate_json_schema)
            if provider_id == "paypal":
                paypal_docs = docs
            elif provider_id == "stripe":
                stripe_docs = docs
            else:
                raise ValueError(f"Unsupported provider: {provider_id}")
        except Exception as exc:
            errors.append((provider_id, exc))

    if errors and fail_on_error:
        provider_id, exc = errors[0]
        if isinstance(exc, PaymentFeeError):
            raise exc
        raise ProviderDataUnavailable(
            provider_id,
            str(exc),
        ) from exc

    engine = PaymentFeeEngine.from_documents(
        paypal=paypal_docs,
        stripe=stripe_docs,
    )
    for provider_id, exc in errors:
        engine._registry.register_error(provider_id, exc)
    return engine
