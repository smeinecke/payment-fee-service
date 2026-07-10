from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import PydanticBaseSettingsSource


class ProviderSettings(BaseModel):
    data_url: str | None = None
    data_path: Path | None = None
    data_ref: str | None = "main"
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
            data_url="https://raw.githubusercontent.com/smeinecke/paypal-fee-data/main",
        ),
        "stripe": ProviderSettings(
            data_url="https://raw.githubusercontent.com/smeinecke/stripe-fee-data/main",
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
