from __future__ import annotations

from payment_fee_service.domain.errors import DataUnavailableError, UnknownProviderError
from payment_fee_service.domain.models import ProviderInfo
from payment_fee_service.providers.base import FeeProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, FeeProvider] = {}
        self._errors: dict[str, str] = {}

    def register(self, provider: FeeProvider) -> None:
        self._providers[provider.provider_id] = provider
        self._errors.pop(provider.provider_id, None)

    def register_error(self, provider_id: str, error: Exception | str) -> None:
        self._errors[provider_id] = str(error)
        self._providers.pop(provider_id, None)

    def get(self, provider_id: str) -> FeeProvider:
        if provider_id in self._errors:
            raise DataUnavailableError(provider_id, self._errors[provider_id])
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise UnknownProviderError(provider_id) from exc

    def infos(self) -> list[ProviderInfo]:
        ids = sorted(set(self._providers) | set(self._errors))
        return [
            ProviderInfo(
                provider=provider_id,
                ready=provider_id in self._providers,
                market_count=(
                    len(self._providers[provider_id].markets())
                    if provider_id in self._providers
                    else 0
                ),
                error=self._errors.get(provider_id),
            )
            for provider_id in ids
        ]

    @property
    def ready(self) -> bool:
        return bool(self._providers) and not self._errors
