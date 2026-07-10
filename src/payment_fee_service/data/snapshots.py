from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ProviderSnapshot:
    provider: str
    schema_version: int
    core: dict[str, Any]
    index: dict[str, Any]
    payment_methods: dict[str, Any] | None = None
    data_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
