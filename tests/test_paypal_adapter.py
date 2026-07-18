from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from payment_fee.errors import DatasetValidationError
from payment_fee.providers.paypal.adapter import adapt_paypal_core_document, adapt_paypal_index_document
from payment_fee.providers.paypal.models import PayPalCoreFees, PayPalIndex

PAYPAL_DATA = Path(__file__).parent.parent.parent / "paypal-fee-data"


def _core_template() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00Z",
        "countries": [
            {
                "country_code": "US",
                "iso_country_code": "US",
                "paypal_market_code": "US",
                "derived_status": "complete",
                "derived": {
                    "status": "complete",
                    "transaction_fee_rules": [],
                    "fixed_fee_schedules": {},
                    "international_surcharge_schedules": {},
                    "maximum_fee_schedules": {},
                    "currency_conversion": None,
                },
            }
        ],
    }


def _index_template() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00Z",
        "countries": [
            {
                "country_code": "US",
                "iso_country_code": "US",
                "paypal_market_code": "US",
                "derived_status": "complete",
                "locale": "en_US",
            }
        ],
    }


class TestCoreAdapter:
    def test_removes_known_crawler_metadata_top_level(self) -> None:
        doc = _core_template()
        for key in ("crawler_revision", "crawl_summary", "coverage_summary", "source_documents", "diagnostics"):
            doc[key] = "should be removed"
        adapted = adapt_paypal_core_document(doc)
        for key in ("crawler_revision", "crawl_summary", "coverage_summary", "source_documents", "diagnostics"):
            assert key not in adapted
        assert adapted["schema_version"] == 1
        assert len(adapted["countries"]) == 1

    def test_unknown_top_level_key_rejected(self) -> None:
        doc = _core_template()
        doc["unknown_metadata_key"] = "bad"
        with pytest.raises(DatasetValidationError) as exc:
            adapt_paypal_core_document(doc)
        assert "unknown_metadata_key" in str(exc.value)

    def test_unknown_country_field_rejected(self) -> None:
        doc = _core_template()
        doc["countries"][0]["unknown_country_field"] = "bad"
        with pytest.raises(DatasetValidationError) as exc:
            adapt_paypal_core_document(doc)
        assert "unknown_country_field" in str(exc.value)

    def test_unknown_derived_field_rejected(self) -> None:
        doc = _core_template()
        doc["countries"][0]["derived"]["unknown_derived_field"] = "bad"
        with pytest.raises(DatasetValidationError) as exc:
            adapt_paypal_core_document(doc)
        assert "unknown_derived_field" in str(exc.value)

    def test_unknown_rule_field_rejected(self) -> None:
        doc = _core_template()
        doc["countries"][0]["derived"]["transaction_fee_rules"] = [
            {
                "id": "goods_and_services",
                "unknown_rule_field": "bad",
                "calculation_status": "calculable",
            }
        ]
        with pytest.raises(DatasetValidationError) as exc:
            adapt_paypal_core_document(doc)
        assert "unknown_rule_field" in str(exc.value)

    def test_unknown_component_field_rejected(self) -> None:
        doc = _core_template()
        doc["countries"][0]["derived"]["transaction_fee_rules"] = [
            {
                "id": "goods_and_services",
                "calculation_status": "calculable",
                "fee_components": [{"type": "percentage", "unknown_component_field": "bad"}],
            }
        ]
        with pytest.raises(DatasetValidationError) as exc:
            adapt_paypal_core_document(doc)
        assert "unknown_component_field" in str(exc.value)

    def test_known_fields_preserved(self) -> None:
        doc = _core_template()
        adapted = adapt_paypal_core_document(doc)
        assert adapted["schema_version"] == 1
        assert adapted["countries"][0]["country_code"] == "US"
        assert adapted["countries"][0]["derived"]["status"] == "complete"

    def test_validates_against_real_schema(self) -> None:
        core_path = PAYPAL_DATA / "json/core-fees.json"
        if not core_path.exists():
            pytest.skip("paypal-fee-data not available")
        doc = json.loads(core_path.read_bytes())
        adapted = adapt_paypal_core_document(doc)
        core = PayPalCoreFees.model_validate(adapted)
        assert core.schema_version == 1


class TestIndexAdapter:
    def test_removes_known_crawler_metadata(self) -> None:
        doc = _index_template()
        doc["coverage_summary"] = "should be removed"
        adapted = adapt_paypal_index_document(doc)
        assert "coverage_summary" not in adapted
        assert adapted["countries"][0]["country_code"] == "US"

    def test_unknown_index_country_field_rejected(self) -> None:
        doc = _index_template()
        doc["countries"][0]["unknown_field"] = "bad"
        with pytest.raises(DatasetValidationError) as exc:
            adapt_paypal_index_document(doc)
        assert "unknown_field" in str(exc.value)

    def test_validates_against_real_schema(self) -> None:
        index_path = PAYPAL_DATA / "json/index.json"
        if not index_path.exists():
            pytest.skip("paypal-fee-data not available")
        doc = json.loads(index_path.read_bytes())
        adapted = adapt_paypal_index_document(doc)
        index = PayPalIndex.model_validate(adapted)
        assert index.schema_version == 1
