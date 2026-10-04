"""Typed immutable models for KEPCO ON parsed data."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any, cast

from .const import (
    CONF_CUSTOMERS,
    CONF_SELECTED_CUSTOMERS,
    DATA_APARTMENT_NAME,
    DATA_CONTRACT_METHOD,
    DATA_CUSTOMER_NUMBER,
    DATA_DONG,
    DATA_HO,
    DATA_HOUSE_CONTRACT_NUMBER,
    DATA_IS_SUPPORTED,
    DATA_STABLE_KEY,
)


@dataclass(frozen=True, slots=True)
class KepcoCookie:
    """A persisted KEPCO ON cookie candidate."""

    name: str
    value: str = field(repr=False)
    domain: str = "online.kepco.co.kr"
    path: str = "/"
    secure: bool = True
    expires: int | None = None
    host_only: bool = True


@dataclass(frozen=True, slots=True)
class KepcoAccountSession:
    """Authenticated account session state safe for coordinator use."""

    refresh_token: str = field(repr=False)
    user_id: str = field(repr=False)
    member_name: str = field(repr=False)
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    token: str | None = field(default=None, repr=False)
    user_mng_seqno: str | None = field(default=None, repr=False)
    cookies: tuple[KepcoCookie, ...] = ()

    def __post_init__(self) -> None:
        updated_at = self.updated_at
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        object.__setattr__(self, "updated_at", updated_at.astimezone(UTC))
        object.__setattr__(self, "cookies", tuple(self.cookies))


@dataclass(frozen=True, slots=True)
class KepcoCustomer:
    """A selectable KEPCO apartment contract."""

    stable_key: str
    apartment_name: str
    dong: str
    ho: str
    contract_method: str
    is_supported: bool
    _customer_number: str = field(repr=False)
    _house_contract_number: str = field(repr=False)
    _change_ymd: str = field(default="", repr=False)

    @property
    def is_house(self) -> bool:
        """Return True for 주택용 direct contracts (non-apartment)."""
        return self.contract_method.startswith("주택용")

    @property
    def change_ymd(self) -> str:
        """Return the contract change date (YYYYMMDD) if known."""
        return self._change_ymd

    @property
    def customer_number(self) -> str:
        """Return the bill request customer number."""
        return self._customer_number

    @property
    def house_contract_number(self) -> str:
        """Return the apartment house contract number."""
        return self._house_contract_number


def _normalized_location_component(value: str) -> str:
    """Normalize a KEPCO dong/ho component for human-readable display."""
    stripped = value.strip()
    if stripped.isdecimal():
        return str(int(stripped))
    return stripped


def customer_location_name(customer: KepcoCustomer) -> str:
    """Return a normalized, apartment-name-free customer location."""
    if customer.is_house:
        return customer.apartment_name
    dong = _normalized_location_component(customer.dong)
    ho = _normalized_location_component(customer.ho)
    return f"{dong}동 {ho}호"


def selected_customer_location_title(customers: Sequence[KepcoCustomer]) -> str:
    """Return the config-entry title for one or more selected customers."""
    if not customers:
        raise ValueError("Selected KEPCO ON customers are unavailable")
    primary = customer_location_name(customers[0])
    if len(customers) == 1:
        return primary
    return f"{primary} 외 {len(customers) - 1}세대"


@dataclass(frozen=True, slots=True)
class KepcoUsageHistoryPoint:
    """One monthly usage/amount history point."""

    month: str
    usage_kwh: int | None = None
    amount_krw: int | None = None


@dataclass(frozen=True, slots=True)
class KepcoChargeBreakdown:
    """Bill charge components."""

    subtotal_krw: int | None = None
    base_krw: int | None = None
    energy_krw: int | None = None
    climate_krw: int | None = None
    fuel_krw: int | None = None
    child_discount_krw: int | None = None
    vat_krw: int | None = None
    fund_krw: int | None = None
    rounding_krw: int | None = None


@dataclass(frozen=True, slots=True)
class KepcoBill:
    """Parsed monthly apartment bill."""

    bill_month: str
    response_bill_month: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    usage_kwh: int | None = None
    household_usage_kwh: int | None = None
    common_usage_kwh: int | None = None
    previous_usage_kwh: int | None = None
    last_year_usage_kwh: int | None = None
    building_average_kwh: int | None = None
    apartment_average_kwh: int | None = None
    current_meter_reading: int | None = None
    previous_meter_reading: int | None = None
    meter_reading_day: str | None = None
    amount_krw: int | None = None
    charge: KepcoChargeBreakdown = field(default_factory=KepcoChargeBreakdown)
    history: tuple[KepcoUsageHistoryPoint, ...] = ()
    current_period_usage_kwh: float | None = None
    predicted_period_usage_kwh: float | None = None
    power_planner_status: str = "not_requested"
    power_planner_return_code: str | None = None
    power_planner_current_status: str | None = None
    power_planner_prediction_status: str | None = None


@dataclass(frozen=True, slots=True)
class KepcoCustomerUpdateResult:
    """Latest update result for one customer."""

    customer: KepcoCustomer
    bill: KepcoBill | None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class KepcoCoordinatorData:
    """Coordinator snapshot consumed by entities."""

    customers: tuple[KepcoCustomer, ...]
    bills_by_customer_key: Mapping[str, KepcoBill] = field(
        default_factory=lambda: MappingProxyType({})
    )
    errors_by_customer_key: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    last_success: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "bills_by_customer_key",
            MappingProxyType(dict(self.bills_by_customer_key)),
        )
        object.__setattr__(
            self,
            "errors_by_customer_key",
            MappingProxyType(dict(self.errors_by_customer_key)),
        )
        if self.last_success is None:
            return
        last_success = self.last_success
        if last_success.tzinfo is None or last_success.utcoffset() is None:
            last_success = last_success.replace(tzinfo=UTC)
        object.__setattr__(self, "last_success", last_success.astimezone(UTC))


def serialize_customer(customer: KepcoCustomer) -> dict[str, Any]:
    """Serialize one selected customer for config-entry storage."""
    return {
        DATA_STABLE_KEY: customer.stable_key,
        DATA_APARTMENT_NAME: customer.apartment_name,
        DATA_DONG: customer.dong,
        DATA_HO: customer.ho,
        DATA_CONTRACT_METHOD: customer.contract_method,
        DATA_IS_SUPPORTED: customer.is_supported,
        DATA_CUSTOMER_NUMBER: customer.customer_number,
        DATA_HOUSE_CONTRACT_NUMBER: customer.house_contract_number,
        "change_ymd": customer.change_ymd,
    }


def _require_nonempty_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid stored KEPCO ON customer field: {key}")
    return value


def deserialize_customer(payload: Mapping[str, Any]) -> KepcoCustomer:
    """Deserialize one strict selected customer config payload."""
    is_supported = payload[DATA_IS_SUPPORTED]
    if not isinstance(is_supported, bool):
        raise ValueError("Invalid stored KEPCO ON customer field: is_supported")
    return KepcoCustomer(
        stable_key=_require_nonempty_str(payload, DATA_STABLE_KEY),
        apartment_name=_require_nonempty_str(payload, DATA_APARTMENT_NAME),
        dong=_require_nonempty_str(payload, DATA_DONG),
        ho=_require_nonempty_str(payload, DATA_HO),
        contract_method=_require_nonempty_str(payload, DATA_CONTRACT_METHOD),
        is_supported=is_supported,
        _customer_number=_require_nonempty_str(payload, DATA_CUSTOMER_NUMBER),
        _house_contract_number=_require_nonempty_str(payload, DATA_HOUSE_CONTRACT_NUMBER),
        _change_ymd=str(payload.get("change_ymd") or ""),
    )


def stored_customers(entry_data: Mapping[str, Any]) -> tuple[KepcoCustomer, ...] | None:
    """Return strict selected customers from config-entry data, if valid."""
    try:
        customers = tuple(
            deserialize_customer(cast("Mapping[str, Any]", payload))
            for payload in entry_data.get(CONF_CUSTOMERS, [])
        )
    except KeyError, TypeError, ValueError:
        return None
    if not customers:
        return None
    return customers


def validate_selected_keys(selected: object, available: Iterable[str]) -> list[str] | None:
    """Validate selected customer keys against available customers."""
    if not isinstance(selected, list) or not selected:
        return None
    available_set = set(available)
    normalized = [str(value) for value in selected]
    if len(set(normalized)) != len(normalized):
        return None
    if any(value not in available_set for value in normalized):
        return None
    return normalized


def selected_customers(
    customers: Sequence[KepcoCustomer], selected: Sequence[str]
) -> tuple[KepcoCustomer, ...]:
    """Return selected customers in selected-key order."""
    by_key = {customer.stable_key: customer for customer in customers}
    return tuple(by_key[key] for key in selected if key in by_key)


def strict_selected_stored_customers(entry_data: Mapping[str, Any]) -> tuple[KepcoCustomer, ...]:
    """Return selected customers from stored data or raise a safe value error."""
    customers = stored_customers(entry_data)
    if customers is None:
        raise ValueError("Stored KEPCO ON customers are unavailable")
    selected = validate_selected_keys(
        entry_data.get(CONF_SELECTED_CUSTOMERS),
        {customer.stable_key for customer in customers},
    )
    if selected is None:
        raise ValueError("Stored KEPCO ON customer selection is invalid")
    result = selected_customers(customers, selected)
    if not result:
        raise ValueError("Stored KEPCO ON customer selection is empty")
    return result


__all__ = [
    "KepcoAccountSession",
    "KepcoBill",
    "KepcoChargeBreakdown",
    "KepcoCookie",
    "KepcoCoordinatorData",
    "KepcoCustomer",
    "KepcoCustomerUpdateResult",
    "KepcoUsageHistoryPoint",
    "customer_location_name",
    "deserialize_customer",
    "selected_customer_location_title",
    "selected_customers",
    "serialize_customer",
    "stored_customers",
    "strict_selected_stored_customers",
    "validate_selected_keys",
]
