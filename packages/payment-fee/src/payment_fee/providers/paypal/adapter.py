from __future__ import annotations

from typing import Any

from payment_fee.errors import DatasetValidationError
from payment_fee.providers.paypal.models import (
    PayPalCoreFees,
    PayPalCountryEntry,
    PayPalCurrencyConversion,
    PayPalDerivedData,
    PayPalFeeComponent,
    PayPalFixedFeeSchedule,
    PayPalIndex,
    PayPalIndexCountry,
    PayPalInternationalSurchargeEntry,
    PayPalInternationalSurchargeSchedule,
    PayPalMaximumFeeSchedule,
    PayPalSource,
    PayPalTransactionFeeRule,
)

_CRAWLER_ONLY_METADATA_KEYS = frozenset(
    {
        "crawler_revision",
        "crawl_summary",
        "coverage_summary",
        "source_documents",
        "diagnostics",
    }
)

_CORE_TOP_ALLOWED = frozenset(PayPalCoreFees.model_fields.keys()) | _CRAWLER_ONLY_METADATA_KEYS
_INDEX_TOP_ALLOWED = frozenset(PayPalIndex.model_fields.keys()) | _CRAWLER_ONLY_METADATA_KEYS
_COUNTRY_ALLOWED = frozenset(PayPalCountryEntry.model_fields.keys()) | _CRAWLER_ONLY_METADATA_KEYS
_INDEX_COUNTRY_ALLOWED = frozenset(PayPalIndexCountry.model_fields.keys()) | _CRAWLER_ONLY_METADATA_KEYS
_DERIVED_ALLOWED = frozenset(PayPalDerivedData.model_fields.keys()) | _CRAWLER_ONLY_METADATA_KEYS
_RULE_ALLOWED = frozenset(PayPalTransactionFeeRule.model_fields.keys())
_COMPONENT_ALLOWED = frozenset(PayPalFeeComponent.model_fields.keys())
_SOURCE_ALLOWED = frozenset(PayPalSource.model_fields.keys())
_FIXED_SCHEDULE_ALLOWED = frozenset(PayPalFixedFeeSchedule.model_fields.keys())
_MAX_SCHEDULE_ALLOWED = frozenset(PayPalMaximumFeeSchedule.model_fields.keys())
_SURCHARGE_SCHEDULE_ALLOWED = frozenset(PayPalInternationalSurchargeSchedule.model_fields.keys())
_SURCHARGE_ENTRY_ALLOWED = frozenset(PayPalInternationalSurchargeEntry.model_fields.keys())
_CURRENCY_CONVERSION_ALLOWED = frozenset(PayPalCurrencyConversion.model_fields.keys())


def _unexpected(path: str, key: str) -> DatasetValidationError:
    return DatasetValidationError(
        f"Unexpected field {key!r} at {path or '<root>'}.",
        path=f"{path}.{key}".lstrip("."),
    )


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DatasetValidationError(
            f"Expected object at {path or '<root>'}.",
            path=path,
        )
    return value


def _filter_keys(value: dict[str, Any], allowed: frozenset[str], path: str) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if key in _CRAWLER_ONLY_METADATA_KEYS:
            continue
        if key not in allowed:
            raise _unexpected(path, key)
        cleaned[key] = item
    return cleaned


def _adapt_source(value: Any, path: str) -> dict[str, Any]:
    return _filter_keys(_require_dict(value, path), _SOURCE_ALLOWED, path)


def _adapt_component(value: Any, path: str) -> dict[str, Any]:
    return _filter_keys(_require_dict(value, path), _COMPONENT_ALLOWED, path)


def _adapt_rule(value: Any, path: str) -> dict[str, Any]:
    rule = _filter_keys(_require_dict(value, path), _RULE_ALLOWED, path)
    if "fee_components" in rule:
        rule["fee_components"] = [
            _adapt_component(c, f"{path}.fee_components[{i}]") for i, c in enumerate(rule["fee_components"])
        ]
    if "source" in rule and rule["source"] is not None:
        rule["source"] = _adapt_source(rule["source"], f"{path}.source")
    return rule


def _adapt_fixed_schedule_entries(value: Any, path: str) -> dict[str, str]:
    entries = _require_dict(value, path)
    for k, v in entries.items():
        if not isinstance(v, str):
            raise DatasetValidationError(
                f"Fixed fee schedule entry {k!r} at {path} must be a string.",
                path=f"{path}.{k}",
            )
    return entries


def _adapt_max_schedule_entries(value: Any, path: str) -> dict[str, str]:
    entries = _require_dict(value, path)
    for k, v in entries.items():
        if not isinstance(v, str):
            raise DatasetValidationError(
                f"Maximum fee schedule entry {k!r} at {path} must be a string.",
                path=f"{path}.{k}",
            )
    return entries


def _adapt_surcharge_entries(value: Any, path: str) -> list[dict[str, Any]]:
    entries = value if isinstance(value, list) else [value]
    return [
        _filter_keys(_require_dict(e, f"{path}[{i}]"), _SURCHARGE_ENTRY_ALLOWED, f"{path}[{i}]")
        for i, e in enumerate(entries)
    ]


def _adapt_schedule_map(
    value: Any,
    path: str,
    schedule_allowed: frozenset[str],
    adapt_entries: Any,
) -> dict[str, dict[str, Any]]:
    schedules = _require_dict(value, path)
    result: dict[str, dict[str, Any]] = {}
    for schedule_id, schedule_value in schedules.items():
        schedule_path = f"{path}.{schedule_id}"
        schedule = _filter_keys(_require_dict(schedule_value, schedule_path), schedule_allowed, schedule_path)
        if "entries" in schedule:
            schedule["entries"] = adapt_entries(schedule["entries"], f"{schedule_path}.entries")
        result[schedule_id] = schedule
    return result


def _adapt_derived(value: Any, path: str) -> dict[str, Any]:
    derived = _filter_keys(_require_dict(value, path), _DERIVED_ALLOWED, path)
    if "transaction_fee_rules" in derived:
        derived["transaction_fee_rules"] = [
            _adapt_rule(r, f"{path}.transaction_fee_rules[{i}]") for i, r in enumerate(derived["transaction_fee_rules"])
        ]
    if "fixed_fee_schedules" in derived:
        derived["fixed_fee_schedules"] = _adapt_schedule_map(
            derived["fixed_fee_schedules"],
            f"{path}.fixed_fee_schedules",
            _FIXED_SCHEDULE_ALLOWED,
            _adapt_fixed_schedule_entries,
        )
    if "international_surcharge_schedules" in derived:
        derived["international_surcharge_schedules"] = _adapt_schedule_map(
            derived["international_surcharge_schedules"],
            f"{path}.international_surcharge_schedules",
            _SURCHARGE_SCHEDULE_ALLOWED,
            _adapt_surcharge_entries,
        )
    if "maximum_fee_schedules" in derived:
        derived["maximum_fee_schedules"] = _adapt_schedule_map(
            derived["maximum_fee_schedules"],
            f"{path}.maximum_fee_schedules",
            _MAX_SCHEDULE_ALLOWED,
            _adapt_max_schedule_entries,
        )
    if "currency_conversion" in derived and derived["currency_conversion"] is not None:
        derived["currency_conversion"] = _filter_keys(
            _require_dict(derived["currency_conversion"], f"{path}.currency_conversion"),
            _CURRENCY_CONVERSION_ALLOWED,
            f"{path}.currency_conversion",
        )
    return derived


def _adapt_country(value: Any, path: str) -> dict[str, Any]:
    country = _filter_keys(_require_dict(value, path), _COUNTRY_ALLOWED, path)
    if "derived" in country:
        country["derived"] = _adapt_derived(country["derived"], f"{path}.derived")
    return country


def _adapt_index_country(value: Any, path: str) -> dict[str, Any]:
    return _filter_keys(_require_dict(value, path), _INDEX_COUNTRY_ALLOWED, path)


def adapt_paypal_core_document(document: Any) -> dict[str, Any]:
    core = _filter_keys(_require_dict(document, ""), _CORE_TOP_ALLOWED, "")
    if "countries" in core:
        core["countries"] = [_adapt_country(c, f"countries[{i}]") for i, c in enumerate(core["countries"])]
    return core


def adapt_paypal_index_document(document: Any) -> dict[str, Any]:
    index = _filter_keys(_require_dict(document, ""), _INDEX_TOP_ALLOWED, "")
    if "countries" in index:
        index["countries"] = [_adapt_index_country(c, f"countries[{i}]") for i, c in enumerate(index["countries"])]
    return index
