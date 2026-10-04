"""Response services for KEPCO ON historical bill data."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime
from typing import Any, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .const import DOMAIN, OPT_HISTORY_MONTHS
from .exceptions import KepcoOnError
from .models import (
    KepcoBill,
    KepcoChargeBreakdown,
    KepcoCoordinatorData,
    KepcoCustomer,
    KepcoUsageHistoryPoint,
)

SERVICE_GET_MONTHLY_BILL = "get_monthly_bill"
SERVICE_GET_USAGE_HISTORY = "get_usage_history"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_CUSTOMER_ID = "customer_id"
ATTR_MONTH = "month"

DEFAULT_SERVICE_HISTORY_MONTHS = 12
_SERVICE_ERROR_KEYS = frozenset({"invalid_entry", "invalid_customer", "invalid_month"})


def _now() -> datetime:
    """Return the current Home Assistant local time."""
    return dt_util.now()


def _response_error(translation_key: str) -> HomeAssistantError:
    """Return a translated service error without sensitive exception text."""
    if translation_key in _SERVICE_ERROR_KEYS:
        return ServiceValidationError(
            translation_key,
            translation_domain=DOMAIN,
            translation_key=translation_key,
        )
    return HomeAssistantError(
        translation_key,
        translation_domain=DOMAIN,
        translation_key=translation_key,
    )


def _validate_month(month: object, *, required: bool) -> str | None:
    """Validate a YYYYMM service month against the supported rolling window."""
    if month is None and not required:
        return None
    if isinstance(month, str) and not month.strip() and not required:
        return None
    if not isinstance(month, str) or len(month) != 6 or not month.isascii() or not month.isdigit():
        raise _response_error("invalid_month")

    year = int(month[:4])
    month_number = int(month[4:])
    if month_number < 1 or month_number > 12:
        raise _response_error("invalid_month")

    now = _now()
    month_index = year * 12 + month_number
    current_index = now.year * 12 + now.month
    earliest_index = current_index - 23
    if month_index > current_index or month_index < earliest_index:
        raise _response_error("invalid_month")
    return month


def _history_months(entry: ConfigEntry) -> int:
    """Return the configured history length with a defensive fallback."""
    try:
        months = int(entry.options.get(OPT_HISTORY_MONTHS, DEFAULT_SERVICE_HISTORY_MONTHS))
    except TypeError, ValueError:
        return DEFAULT_SERVICE_HISTORY_MONTHS
    if months < 1 or months > 24:
        return DEFAULT_SERVICE_HISTORY_MONTHS
    return months


def _entry_from_call(hass: HomeAssistant, entry_id: str) -> ConfigEntry:
    """Resolve and validate a config entry for a service call."""
    entry = hass.config_entries.async_get_known_entry(entry_id)
    if (
        entry is None
        or getattr(entry, "domain", None) != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
    ):
        raise _response_error("invalid_entry")

    runtime_data = getattr(entry, "runtime_data", None)
    session = getattr(runtime_data, "session", None)
    coordinator = getattr(runtime_data, "coordinator", None)
    data = getattr(coordinator, "data", None)
    client = getattr(runtime_data, "client", None)
    if (
        runtime_data is None
        or client is None
        or coordinator is None
        or data is None
        or session is None
        or getattr(session, "closed", True)
        or not isinstance(data, KepcoCoordinatorData)
    ):
        raise _response_error("invalid_entry")
    return entry


def _customer_from_entry(entry: ConfigEntry, customer_id: str) -> KepcoCustomer:
    """Return a selected customer by stable hashed key."""
    data = cast("KepcoCoordinatorData", entry.runtime_data.coordinator.data)
    for customer in data.customers:
        if customer.stable_key == customer_id:
            return customer
    raise _response_error("invalid_customer")


def _serialize_bill(bill: KepcoBill) -> dict[str, Any]:
    """Serialize a bill into the service response contract."""
    return {
        "billing_month": bill.bill_month,
        "usage_period_start": bill.period_start.isoformat() if bill.period_start else None,
        "usage_period_end": bill.period_end.isoformat() if bill.period_end else None,
        "usage_kwh": bill.usage_kwh,
        "amount_due_krw": bill.amount_krw,
        "charge_breakdown": _serialize_charge(bill.charge),
    }


def _serialize_charge(charge: KepcoChargeBreakdown) -> dict[str, int]:
    """Serialize non-null named charge fields."""
    return {
        field.name: value
        for field in fields(KepcoChargeBreakdown)
        if (value := getattr(charge, field.name)) is not None
    }


def _serialize_history_point(point: KepcoUsageHistoryPoint) -> dict[str, Any]:
    """Serialize one history point; the billed amount is included only when known."""
    serialized: dict[str, Any] = {"month": point.month, "usage_kwh": point.usage_kwh}
    if point.amount_krw is not None:
        serialized["amount_krw"] = point.amount_krw
    return serialized


def _serialize_history(bill: KepcoBill, limit: int) -> dict[str, Any]:
    """Serialize bill history in ascending month order and selected length."""
    points = sorted(bill.history, key=lambda point: point.month)
    return {"history": [_serialize_history_point(point) for point in points[-limit:]]}


def _base_schema(*, month_required: bool) -> vol.Schema:
    """Return a service schema for required service fields."""
    month_key: vol.Required | vol.Optional
    if month_required:
        month_key = vol.Required(ATTR_MONTH)
        month_validator: Any = str
    else:
        month_key = vol.Optional(ATTR_MONTH, default=None)
        month_validator = vol.Any(None, str)
    return vol.Schema(
        {
            vol.Required(ATTR_CONFIG_ENTRY_ID): str,
            vol.Required(ATTR_CUSTOMER_ID): str,
            month_key: month_validator,
        }
    )


async def _async_get_monthly_bill(call: ServiceCall) -> dict[str, Any]:
    """Return one monthly bill response."""
    entry = _entry_from_call(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    customer = _customer_from_entry(entry, call.data[ATTR_CUSTOMER_ID])
    month = _validate_month(call.data[ATTR_MONTH], required=True)
    bill: KepcoBill | None = None
    service_failed = False
    try:
        bill = await entry.runtime_data.client.async_get_bill(customer, month)
    except KepcoOnError:
        service_failed = True
    if service_failed:
        raise _response_error("service_failed")
    if bill is None:
        raise _response_error("service_failed")
    return _serialize_bill(bill)


async def _async_get_usage_history(call: ServiceCall) -> dict[str, Any]:
    """Return selected customer usage history."""
    entry = _entry_from_call(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    customer = _customer_from_entry(entry, call.data[ATTR_CUSTOMER_ID])
    month = _validate_month(call.data.get(ATTR_MONTH), required=False)

    bill: KepcoBill | None = None
    if month is None:
        data = cast("KepcoCoordinatorData", entry.runtime_data.coordinator.data)
        bill = data.bills_by_customer_key.get(customer.stable_key)
    if bill is None:
        service_failed = False
        try:
            bill = await entry.runtime_data.client.async_get_bill(customer, month)
        except KepcoOnError:
            service_failed = True
        if service_failed:
            raise _response_error("service_failed")
        if bill is None:
            raise _response_error("service_failed")
    return _serialize_history(bill, _history_months(entry))


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register KEPCO ON response service actions."""
    if not hass.services.has_service(DOMAIN, SERVICE_GET_MONTHLY_BILL):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_MONTHLY_BILL,
            _async_get_monthly_bill,
            schema=_base_schema(month_required=True),
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_GET_USAGE_HISTORY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_USAGE_HISTORY,
            _async_get_usage_history,
            schema=_base_schema(month_required=False),
            supports_response=SupportsResponse.ONLY,
        )


__all__ = [
    "ATTR_CONFIG_ENTRY_ID",
    "ATTR_CUSTOMER_ID",
    "ATTR_MONTH",
    "SERVICE_GET_MONTHLY_BILL",
    "SERVICE_GET_USAGE_HISTORY",
    "async_setup_services",
]
