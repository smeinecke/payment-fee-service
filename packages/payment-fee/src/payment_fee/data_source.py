from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from payment_fee.data import validate_json_schema
from payment_fee.errors import DatasetValidationError, ProviderDataUnavailable


def _default_cache_dir() -> Path:
    base = Path.home() / ".cache"
    if not base.exists():
        import tempfile

        return Path(tempfile.gettempdir()) / "payment-fee"
    return base / "payment-fee"


def _pinned_data_refs() -> dict[str, str]:
    """Return pinned data refs from the repository contracts, if available."""
    # package is at packages/payment-fee/src/payment_fee; repo root is 5 parents up
    contract = Path(__file__).resolve().parents[5] / "contracts" / "data-revisions.json"
    with contextlib.suppress(Exception):
        if contract.is_file():
            document = json.loads(contract.read_text(encoding="utf-8"))
            return {name: spec.get("ref", "main") for name, spec in document.get("revisions", {}).items()}
    return {}


_PINNED_REFS = _pinned_data_refs()

DEFAULT_PAYPAL_URL = "https://raw.githubusercontent.com/smeinecke/paypal-fee-data/{data_ref}"
DEFAULT_STRIPE_URL = "https://raw.githubusercontent.com/smeinecke/stripe-fee-data/{data_ref}"


@dataclass(frozen=True, slots=True)
class DataLocation:
    provider: str
    local_path: Path | None = None
    base_url: str | None = None
    data_ref: str | None = None


class JsonDataSource:
    """Load provider JSON documents from a local path or remote URL with TTL caching."""

    def __init__(
        self,
        location: DataLocation,
        cache_dir: Path | str | None = None,
        ttl_seconds: float = 86400.0,
        auto_refresh: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.location = location
        self.ttl_seconds = ttl_seconds
        self.auto_refresh = auto_refresh
        self.timeout_seconds = timeout_seconds
        if cache_dir is None:
            self.cache_dir = _default_cache_dir()
        else:
            self.cache_dir = Path(cache_dir)

    def read_bytes(self, relative_path: str) -> bytes:
        if self.location.local_path is not None:
            return (self.location.local_path / relative_path).read_bytes()
        if not self.location.base_url:
            raise ProviderDataUnavailable(self.location.provider, "No data source configured.")
        return self._read_remote(relative_path)

    def read_json(self, relative_path: str) -> dict[str, Any]:
        return json.loads(self.read_bytes(relative_path))

    def validate(self, document: dict[str, Any], schema_path: str) -> None:
        schema = self.read_json(schema_path)
        validate_json_schema(document, schema, schema_path)

    def refresh(self, relative_path: str) -> bytes:
        """Force a re-download of ``relative_path`` by invalidating the cache."""
        cache_path = self._cache_path(relative_path)
        cache_path.unlink(missing_ok=True)
        return self._read_remote(relative_path, force=True)

    def _base_url(self) -> str:
        base = self.location.base_url or ""
        if "{data_ref}" in base:
            return base.replace("{data_ref}", self.location.data_ref or "main")
        return base

    def _cache_path(self, relative_path: str) -> Path:
        ref = self.location.data_ref or "default"
        cache = self.cache_dir / self.location.provider / ref / relative_path
        cache.parent.mkdir(parents=True, exist_ok=True)
        return cache

    def _read_remote(self, relative_path: str, force: bool = False) -> bytes:
        try:
            import httpx
        except ImportError as exc:
            raise DatasetValidationError(
                "httpx is required for remote data sources; install payment-fee[http].",
                provider=self.location.provider,
            ) from exc

        cache_path = self._cache_path(relative_path)
        has_cache = cache_path.is_file()
        if has_cache and (self._is_fresh(cache_path) or (not force and not self.auto_refresh)):
            return cache_path.read_bytes()

        base_url = self._base_url()
        url = f"{base_url.rstrip('/')}/{relative_path.lstrip('/')}"
        try:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
                payload = response.content
        except Exception as exc:
            if has_cache:
                return cache_path.read_bytes()
            raise ProviderDataUnavailable(
                self.location.provider,
                f"Failed to download {url}: {exc}",
            ) from exc

        cache_path.write_bytes(payload)
        return payload

    def _is_fresh(self, cache_path: Path) -> bool:
        return time.time() - cache_path.stat().st_mtime < self.ttl_seconds


def _location_from_string(provider: str, value: str, data_ref: str | None = None) -> DataLocation:
    if value.startswith(("http://", "https://")):
        if data_ref is None:
            data_ref = _PINNED_REFS.get(provider, "main")
        return DataLocation(provider=provider, base_url=value, data_ref=data_ref)
    return DataLocation(provider=provider, local_path=Path(value), data_ref=data_ref or "local")


def _load_documents(
    source: JsonDataSource,
    validate: bool,
) -> dict[str, Any]:
    core = source.read_json("json/core-fees.json")
    index = source.read_json("json/index.json")
    if validate:
        source.validate(core, "schemas/core-fees-v1.schema.json")
        source.validate(index, "schemas/index-v1.schema.json")
    return {"core": core, "index": index}
