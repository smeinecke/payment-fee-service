from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import PydanticBaseSettingsSource


def _data_revisions_file_candidates() -> list[Path]:
    """Candidate locations for ``contracts/data-revisions.json``.

    The search order is:
    1. ``PAYMENT_FEE_DATA_REVISIONS_FILE`` environment variable.
    2. Repository checkout layout (local development).
    3. Standard Docker image layout at ``/app/contracts/data-revisions.json``.
    """
    candidates: list[Path] = []
    env_path = os.environ.get("PAYMENT_FEE_DATA_REVISIONS_FILE")
    if env_path:
        candidates.append(Path(env_path))
    with contextlib.suppress(IndexError):
        candidates.append(Path(__file__).resolve().parents[4] / "contracts" / "data-revisions.json")
    candidates.append(Path("/app/contracts/data-revisions.json"))
    return candidates


def _pinned_data_refs() -> dict[str, str]:
    """Return the pinned data-repo refs from contracts/data-revisions.json.

    Falls back to ``main`` only in development when no contract file is found.
    """
    for contract_path in _data_revisions_file_candidates():
        if not contract_path.is_file():
            continue
        try:
            document = json.loads(contract_path.read_text(encoding="utf-8"))
            return {name: spec.get("ref", "main") for name, spec in document.get("revisions", {}).items()}
        except Exception:
            continue
    return {}


_PINNED_REFS = _pinned_data_refs()


class ProviderSettings(BaseModel):
    data_url: str | None = None
    data_path: Path | None = None
    data_ref: str | None = None
    enabled: bool = True


class JsonConfigSettingsSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        config_file = os.environ.get("PAYMENT_FEE_CONFIG_FILE")
        if not config_file:
            return {}
        path = Path(config_file)
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in config file {config_file}: {exc}") from exc


def _default_providers() -> dict[str, ProviderSettings]:
    return {
        "paypal": ProviderSettings(
            data_url="https://raw.githubusercontent.com/smeinecke/paypal-fee-data/{data_ref}",
            data_ref=_PINNED_REFS.get("paypal", "main"),
        ),
        "stripe": ProviderSettings(
            data_url="https://raw.githubusercontent.com/smeinecke/stripe-fee-data/{data_ref}",
            data_ref=_PINNED_REFS.get("stripe", "main"),
        ),
    }


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PAYMENT_FEE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"
    providers: dict[str, ProviderSettings] = Field(default_factory=_default_providers)
    refresh_interval_seconds: float = 86400
    admin_token: str | None = None
    http_timeout_seconds: float = 30
    fail_startup_on_data_error: bool = False
    validate_json_schema: bool = True

    @model_validator(mode="before")
    @classmethod
    def _parse_providers_json(cls, values: Any) -> Any:
        if isinstance(values, dict):
            providers = values.get("providers")
            if isinstance(providers, str):
                try:
                    values["providers"] = json.loads(providers)
                except json.JSONDecodeError as exc:
                    raise ValueError("PAYMENT_FEE_PROVIDERS must be valid JSON") from exc
        return values

    @model_validator(mode="after")
    def _require_pinned_data_refs(self) -> Settings:
        """Require pinned data refs in non-development environments.

        In development, falling back to the floating ``main`` branch is allowed
        for convenience. In production-like environments the data revision must
        be pinned via ``contracts/data-revisions.json``,
        ``PAYMENT_FEE_DATA_REVISIONS_FILE``, or an explicit ``PAYMENT_FEE_PROVIDERS``
        value so that fee calculations are reproducible.
        """
        if self.environment == "development":
            return self
        for provider_id, provider in (self.providers or {}).items():
            if not provider.enabled:
                continue
            if provider.data_path is not None:
                continue
            if provider.data_ref in (None, "main"):
                raise ValueError(
                    f"Provider {provider_id!r} data_ref must be pinned in environment "
                    f"{self.environment!r}. Set PAYMENT_FEE_DATA_REVISIONS_FILE, "
                    "provide a contracts/data-revisions.json file, or set PAYMENT_FEE_PROVIDERS "
                    "with explicit data_ref values."
                )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            JsonConfigSettingsSource(settings_cls),
            file_secret_settings,
        )
