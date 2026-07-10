from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator


@dataclass(frozen=True, slots=True)
class DataLocation:
    provider: str
    local_path: Path | None
    base_url: str | None
    data_ref: str | None = None


class JsonDataSource:
    def __init__(self, location: DataLocation, timeout_seconds: float = 30) -> None:
        self.location = location
        self.timeout_seconds = timeout_seconds

    def read_bytes(self, relative_path: str) -> bytes:
        if self.location.local_path is not None:
            return (self.location.local_path / relative_path).read_bytes()
        if not self.location.base_url:
            raise FileNotFoundError(f"No data source configured for {self.location.provider}")
        url = f"{self.location.base_url.rstrip('/')}/{relative_path.lstrip('/')}"
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content

    def read_json(self, relative_path: str) -> dict[str, Any]:
        return json.loads(self.read_bytes(relative_path))

    def validate(self, document: dict[str, Any], schema_path: str) -> None:
        schema = self.read_json(schema_path)
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
        if errors:
            first = errors[0]
            path = ".".join(str(part) for part in first.path)
            raise ValueError(f"Schema validation failed at {path or '<root>'}: {first.message}")

    @staticmethod
    def sha256_bytes(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()
