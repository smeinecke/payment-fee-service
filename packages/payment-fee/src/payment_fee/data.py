from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, BinaryIO

from payment_fee.errors import DatasetValidationError


def load_json(value: str | Path | bytes | BinaryIO | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, Path)):
        path = Path(value)
        if path.is_file():
            return json.loads(path.read_bytes())
        if path.is_dir():
            raise DatasetValidationError(
                "Expected a file path, got a directory.",
                path=str(path),
            )
        raise DatasetValidationError(
            "JSON file not found.",
            path=str(path),
        )
    if isinstance(value, bytes):
        return json.loads(value)
    if hasattr(value, "read"):
        return json.load(value)
    raise DatasetValidationError(
        "Unsupported document type.",
        type=str(type(value)),
    )


def validate_json_schema(
    document: dict[str, Any],
    schema: dict[str, Any] | str | Path | None,
    name: str = "schema",
) -> None:
    if schema is None:
        return
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise DatasetValidationError(
            "jsonschema is required for schema validation.",
            name=name,
        ) from exc

    schema_data = load_json(schema) if isinstance(schema, (str, Path)) else schema
    validator = Draft202012Validator(schema_data)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.path)
        raise DatasetValidationError(
            f"Schema validation failed for {name} at {path or '<root>'}: {first.message}",
            name=name,
            path=path,
        )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
