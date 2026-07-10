from __future__ import annotations

from payment_fee_service.data.snapshots import ProviderSnapshot
from payment_fee_service.data.source import JsonDataSource


class StripeRepository:
    def __init__(self, source: JsonDataSource, validate_schema: bool = True) -> None:
        self.source = source
        self.validate_schema = validate_schema

    def load(self) -> ProviderSnapshot:
        core = self.source.read_json("json/core-fees.json")
        index = self.source.read_json("json/index.json")
        payment_methods = self.source.read_json("json/payment-methods.json")
        if self.validate_schema:
            self.source.validate(core, "schemas/core-fees-v1.schema.json")
            self.source.validate(index, "schemas/index-v1.schema.json")
            self.source.validate(payment_methods, "schemas/payment-methods-v1.schema.json")
        schema_version = int(core.get("schema_version", 1))
        if schema_version != 1:
            raise ValueError(f"Unsupported Stripe core schema version: {schema_version}")
        if not isinstance(core.get("markets"), list):
            raise ValueError("Stripe core-fees.json has no markets array")
        return ProviderSnapshot(
            provider="stripe",
            schema_version=schema_version,
            core=core,
            index=index,
            payment_methods=payment_methods,
            data_ref=self.source.location.data_ref,
        )
