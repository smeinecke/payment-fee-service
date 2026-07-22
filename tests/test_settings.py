from __future__ import annotations

import json
from pathlib import Path

import pytest
from payment_fee_service.settings import Settings


def test_default_providers() -> None:
    settings = Settings()
    assert "paypal" in settings.providers
    assert "stripe" in settings.providers
    assert settings.providers["paypal"].data_url
    assert settings.providers["stripe"].data_url
    assert settings.refresh_interval_seconds == 86400
    assert settings.admin_token is None


def test_providers_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAYMENT_FEE_CONFIG_FILE", raising=False)
    monkeypatch.setenv(
        "PAYMENT_FEE_PROVIDERS",
        '{"paypal": {"data_url": "https://example.com/paypal", "data_ref": "test"}}',
    )
    settings = Settings()
    assert settings.providers["paypal"].data_url == "https://example.com/paypal"
    assert settings.providers["paypal"].data_ref == "test"
    assert "stripe" not in settings.providers


def test_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "refresh_interval_seconds": 3600,
                "admin_token": "from-file",
                "providers": {
                    "paypal": {
                        "data_url": "https://example.com/paypal",
                        "data_ref": "pinned",
                        "enabled": False,
                    },
                },
            }
        )
    )
    monkeypatch.setenv("PAYMENT_FEE_CONFIG_FILE", str(config_file))
    settings = Settings()
    assert settings.refresh_interval_seconds == 3600
    assert settings.admin_token == "from-file"
    assert settings.providers["paypal"].data_url == "https://example.com/paypal"
    assert settings.providers["paypal"].data_ref == "pinned"
    assert settings.providers["paypal"].enabled is False


def test_env_overrides_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"refresh_interval_seconds": 3600, "admin_token": "from-file"}))
    monkeypatch.setenv("PAYMENT_FEE_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("PAYMENT_FEE_ADMIN_TOKEN", "from-env")
    settings = Settings()
    assert settings.admin_token == "from-env"


def test_production_requires_pinned_data_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYMENT_FEE_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "PAYMENT_FEE_PROVIDERS",
        '{"paypal": {"data_url": "https://example.com/paypal", "data_ref": "main"}, '
        '"stripe": {"data_url": "https://example.com/stripe", "data_ref": "main"}}',
    )
    with pytest.raises(ValueError, match="data_ref must be pinned"):
        Settings()


def test_production_allows_pinned_data_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYMENT_FEE_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "PAYMENT_FEE_PROVIDERS",
        '{"paypal": {"data_url": "https://example.com/paypal", "data_ref": "abc123"}, '
        '"stripe": {"data_url": "https://example.com/stripe", "data_ref": "def456"}}',
    )
    settings = Settings()
    assert settings.providers["paypal"].data_ref == "abc123"
    assert settings.providers["stripe"].data_ref == "def456"
