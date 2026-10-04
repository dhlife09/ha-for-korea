"""Diagnostics support for the KEPCO ON integration."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Any, cast

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_POLLING_INTERVAL_HOURS,
    OPT_POLLING_INTERVAL_HOURS,
    VERSION,
)
from .models import KepcoBill, KepcoChargeBreakdown, KepcoCoordinatorData, KepcoUsageHistoryPoint

DENY_KEYS = frozenset(
    {
        "access_token",
        "account_uid_hash",
        "address",
        "amount",
        "amount_due",
        "amount_due_krw",
        "amount_krw",
        "apartment",
        "apartment_name",
        "api_body",
        "body",
        "charge",
        "contract",
        "contract_method",
        "contract_number",
        "cookie",
        "cookies",
        "customer",
        "customer_id",
        "customer_number",
        "customers",
        "cust_no",
        "dong",
        "email",
        "history",
        "ho",
        "house_contract_number",
        "member",
        "member_name",
        "membername",
        "password",
        "phone",
        "raw",
        "refresh_token",
        "session",
        "session_handoff",
        "token",
        "usage",
        "usage_kwh",
        "user_id",
        "userid",
        "username",
        "value",
    }
)

DENY_KEY_PARTS = frozenset(
    {
        "address",
        "amount",
        "apartment",
        "body",
        "charge",
        "contract",
        "cookie",
        "customer",
        "dong",
        "email",
        "history",
        "house",
        "member",
        "password",
        "phone",
        "raw",
        "session",
        "token",
        "usage",
        "user",
    }
)

SAFE_BILL_FIELDS = (
    "amount_krw",
    "apartment_average_kwh",
    "bill_month",
    "building_average_kwh",
    "charge",
    "current_meter_reading",
    "history",
    "household_usage_kwh",
    "common_usage_kwh",
    "last_year_usage_kwh",
    "meter_reading_day",
    "period_end",
    "period_start",
    "previous_meter_reading",
    "previous_usage_kwh",
    "response_bill_month",
    "usage_kwh",
)
SAFE_CHARGE_FIELDS = (
    "base_krw",
    "child_discount_krw",
    "climate_krw",
    "energy_krw",
    "fuel_krw",
    "fund_krw",
    "rounding_krw",
    "subtotal_krw",
    "vat_krw",
)
SAFE_HISTORY_FIELDS = ("amount_krw", "month", "usage_kwh")
SAFE_ERROR_CATEGORIES = frozenset(
    {
        "api_error",
        "no_customers",
        "protocol_error",
        "unknown_error",
    }
)
SUMMARY_SAFE_KEYS = frozenset(
    {
        "account_type",
        "availability",
        "bill",
        "bill_errors",
        "bills",
        "charge",
        "config_entry",
        "customers",
        "error_categories",
        "history",
        "home_assistant_version",
        "integration",
        "last_success",
        "last_update_success",
        "loaded",
        "parsed_fields",
        "polling_interval_hours",
        "runtime",
        "selected_customer_ids",
        "selected_customer_count",
        "version",
    }
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
LONG_ID_RE = re.compile(r"\b\d{8,}\b")
PHONE_RE = re.compile(r"\b(?:\+?82[-.\s]?)?0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4}\b")
STABLE_CUSTOMER_HASH_RE = re.compile(r"[0-9a-f]{64}")
SECRET_MARKERS = frozenset(
    {
        "access_secret",
        "cookie_secret",
        "customer_secret",
        "jwt_secret",
        "member_secret",
        "password_secret",
        "phone_secret",
        "raw_body_secret",
        "secret_canary",
        "token_secret",
        "user_id_secret",
    }
)


def _deny_key(key: object) -> bool:
    """Return whether a key name is unsafe for diagnostics."""
    normalized = str(key).replace("-", "_").lower()
    return normalized in DENY_KEYS or any(part in normalized for part in DENY_KEY_PARTS)


def _unsafe_string(value: str) -> bool:
    """Return whether a string value looks sensitive or user-specific."""
    if STABLE_CUSTOMER_HASH_RE.fullmatch(value) is not None:
        return False
    lowered = value.lower()
    return (
        any(marker in lowered for marker in SECRET_MARKERS)
        or EMAIL_RE.search(value) is not None
        or JWT_RE.search(value) is not None
        or PHONE_RE.search(value) is not None
        or LONG_ID_RE.search(value) is not None
    )


def _sanitize(value: Any, *, protected_keys: frozenset[str] = frozenset()) -> Any:
    """Return a JSON-serializable value after recursively dropping denied keys."""
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item, protected_keys=protected_keys)
            for key, item in value.items()
            if str(key) in protected_keys or not _deny_key(key)
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_sanitize(item, protected_keys=protected_keys) for item in value]
    if isinstance(value, str):
        return "[redacted]" if _unsafe_string(value) else value
    if isinstance(value, int | float | bool) or value is None:
        return value
    return str(type(value).__name__)


def _json_safe(value: Any) -> Any:
    """Return a JSON-serializable value without applying privacy key filtering."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(type(value).__name__)


def _safe_len(value: Any) -> int:
    """Return length for sized values without surfacing their contents."""
    try:
        return len(value)
    except TypeError:
        return 0


def _entry_selected_customer_count(entry: Any) -> int:
    """Return selected customer count from config data safely."""
    selected = getattr(entry, "data", {}).get("selected_customers", [])
    return _safe_len(selected) if isinstance(selected, list) else 0


def _selected_customer_ids(data: KepcoCoordinatorData | None) -> list[str]:
    """Return response-action-safe customer hashes from loaded coordinator data."""
    if data is None:
        return []
    selected: list[str] = []
    for customer in data.customers:
        stable_key = getattr(customer, "stable_key", None)
        if isinstance(stable_key, str) and STABLE_CUSTOMER_HASH_RE.fullmatch(stable_key):
            selected.append(stable_key)
    return selected


def _polling_interval_hours(entry: Any) -> int:
    """Return configured polling interval, falling back safely."""
    options = getattr(entry, "options", {})
    if not isinstance(options, Mapping):
        return DEFAULT_POLLING_INTERVAL_HOURS
    value = options.get(OPT_POLLING_INTERVAL_HOURS, DEFAULT_POLLING_INTERVAL_HOURS)
    try:
        return int(value)
    except TypeError, ValueError:
        return DEFAULT_POLLING_INTERVAL_HOURS


def _iso_or_none(value: Any) -> str | None:
    """Return an ISO timestamp if available."""
    isoformat = getattr(value, "isoformat", None)
    if not callable(isoformat):
        return None
    return cast("str", isoformat())


def _error_categories(errors: Any) -> dict[str, int]:
    """Return counts of safe error categories."""
    if not isinstance(errors, Mapping):
        return {}
    categories: Counter[str] = Counter()
    for value in errors.values():
        category = (
            value if isinstance(value, str) and value in SAFE_ERROR_CATEGORIES else "unknown_error"
        )
        categories[category] += 1
    return dict(sorted(categories.items()))


def _coordinator_data(runtime_data: Any) -> KepcoCoordinatorData | None:
    """Return coordinator data when it has the expected snapshot type."""
    coordinator = getattr(runtime_data, "coordinator", None)
    data = getattr(coordinator, "data", None)
    return data if isinstance(data, KepcoCoordinatorData) else None


def _dataclass_field_names(cls: type[Any], allowed: tuple[str, ...]) -> list[str]:
    """Return stable allowed dataclass field names."""
    if not is_dataclass(cls):
        return []
    existing = {field.name for field in fields(cls)}
    return [name for name in allowed if name in existing]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: Any,
) -> dict[str, Any]:
    """Return safe diagnostics for a KEPCO ON config entry."""
    redacted_source = async_redact_data(
        {
            "data": getattr(entry, "data", {}),
            "options": getattr(entry, "options", {}),
            "runtime": getattr(entry, "runtime_data", None),
        },
        DENY_KEYS,
    )
    sanitized_source = _sanitize(redacted_source)

    runtime_data = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime_data, "coordinator", None)
    data = _coordinator_data(runtime_data)

    summary = {
        "integration": {
            "version": VERSION,
            "home_assistant_version": str(
                getattr(getattr(hass, "config", None), "version", HA_VERSION)
            ),
        },
        "config_entry": {
            "account_type": "INDI",
            "polling_interval_hours": _polling_interval_hours(entry),
            "selected_customer_count": _entry_selected_customer_count(entry),
            "selected_customer_ids": _selected_customer_ids(data),
        },
        "runtime": {
            "loaded": runtime_data is not None,
            "last_update_success": getattr(coordinator, "last_update_success", None),
            "last_success": _iso_or_none(getattr(data, "last_success", None)),
        },
        "availability": {
            "customers": _safe_len(getattr(data, "customers", ())),
            "bills": _safe_len(getattr(data, "bills_by_customer_key", {})),
            "bill_errors": _safe_len(getattr(data, "errors_by_customer_key", {})),
        },
        "parsed_fields": {
            "bill": _dataclass_field_names(KepcoBill, SAFE_BILL_FIELDS),
            "charge": _dataclass_field_names(KepcoChargeBreakdown, SAFE_CHARGE_FIELDS),
            "history": _dataclass_field_names(KepcoUsageHistoryPoint, SAFE_HISTORY_FIELDS),
        },
        "error_categories": _error_categories(getattr(data, "errors_by_customer_key", {})),
    }
    sanitized_summary = _sanitize(summary, protected_keys=SUMMARY_SAFE_KEYS)
    if not isinstance(sanitized_source, dict) or not isinstance(sanitized_summary, dict):
        return {}
    return cast("dict[str, Any]", _json_safe(sanitized_summary))


__all__ = ["async_get_config_entry_diagnostics"]
