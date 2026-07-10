from __future__ import annotations

import importlib
from types import ModuleType


class ProviderModuleError(ImportError):
    def __init__(self, provider_id: str) -> None:
        super().__init__(f"Could not load provider module for {provider_id!r}")
        self.provider_id = provider_id


def load_provider_module(provider_id: str) -> ModuleType:
    module_name = f"payment_fee_service.providers.{provider_id}"
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise ProviderModuleError(provider_id) from exc
