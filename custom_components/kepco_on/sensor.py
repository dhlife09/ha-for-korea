"""Sensor platform for KEPCO ON selected customer bills."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import RegistryEntry, RegistryEntryDisabler
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import KepcoOnConfigEntry
from .const import (
    COMBINED_APARTMENT_PLANNER_CONTRACT,
    DEFAULT_CO2_FACTOR_KG_PER_KWH,
    DOMAIN,
    OPT_CO2_FACTOR_KG_PER_KWH,
    PAGE_URL,
    VERSION,
)
from .coordinator import KepcoOnDataUpdateCoordinator
from .models import KepcoBill, KepcoCustomer

KepcoSensorValue = str | int | float | date | None
KepcoSensorAttributes = dict[str, str | date | int | None]
KepcoValueFunction = Callable[[KepcoBill, dict[str, Any]], KepcoSensorValue]

POWER_PLANNER_FIELDS = {
    "current_period_usage": "F_AP_QT",
    "predicted_period_usage": "PREDICT_TOT",
}
POWER_PLANNER_MESSAGES = {
    "ok": "한전 파워플래너에서 사용량을 수신했습니다.",
    "no_data": "한전 파워플래너가 해당 계약의 사용량을 제공하지 않았습니다.",
    "not_requested": "현재 검침기간 사용량을 아직 조회하지 않았습니다.",
    "rate_limited": "한전 요청 제한으로 부가 사용량 조회를 완료하지 못했습니다.",
    "connection_error": "한전 파워플래너 연결에 실패했습니다.",
    "invalid_response": "한전 파워플래너 응답을 해석하지 못했습니다.",
    "source_unit_unverified": "예측 필드의 kWh 단위가 확인되지 않아 사용량으로 표시하지 않습니다.",
}


class KepcoDeviceGroup(StrEnum):
    """Logical Home Assistant devices created for one KEPCO customer."""

    MONTHLY_USAGE = "monthly_usage"
    METER_USAGE = "meter_usage"
    ELECTRICITY_CHARGE = "electricity_charge"
    NEIGHBOR_COMPARISON = "neighbor_comparison"
    GREENHOUSE_GAS = "greenhouse_gas"


_DEVICE_GROUP_NAMES: dict[KepcoDeviceGroup, str] = {
    KepcoDeviceGroup.MONTHLY_USAGE: "월별 사용량",
    KepcoDeviceGroup.METER_USAGE: "검침/전기사용량",
    KepcoDeviceGroup.ELECTRICITY_CHARGE: "전기요금",
    KepcoDeviceGroup.NEIGHBOR_COMPARISON: "이웃 전기사용량 비교",
    KepcoDeviceGroup.GREENHOUSE_GAS: "온실가스 배출량",
}


@dataclass(frozen=True, kw_only=True)
class KepcoSensorEntityDescription(SensorEntityDescription):
    """Describe one KEPCO ON sensor value and its logical device group."""

    device_group: KepcoDeviceGroup
    value_fn: KepcoValueFunction
    history_month_offset: int | None = None


def _usage(field: str) -> KepcoValueFunction:
    """Return a bill usage accessor."""

    def value(bill: KepcoBill, options: dict[str, Any]) -> KepcoSensorValue:
        del options
        field_value = getattr(bill, field)
        if isinstance(field_value, str | int | date) or field_value is None:
            return field_value
        return None

    return value


def _charge(field: str) -> KepcoValueFunction:
    """Return a bill charge accessor."""

    def value(bill: KepcoBill, options: dict[str, Any]) -> KepcoSensorValue:
        del options
        field_value = getattr(bill.charge, field)
        if isinstance(field_value, str | int | date) or field_value is None:
            return field_value
        return None

    return value


def _shift_month(month: str, offset: int) -> str:
    """Return YYYYMM shifted by the requested number of calendar months."""
    month_index = int(month[:4]) * 12 + int(month[4:]) - 1 + offset
    year, zero_based_month = divmod(month_index, 12)
    return f"{year:04d}{zero_based_month + 1:02d}"


def _history_usage(month_offset: int) -> KepcoValueFunction:
    """Return a monthly history accessor using stable relative slots."""

    def value(bill: KepcoBill, options: dict[str, Any]) -> int | None:
        del options
        direct_values = {
            0: bill.usage_kwh,
            -1: bill.previous_usage_kwh,
            -12: bill.last_year_usage_kwh,
        }
        direct_value = direct_values.get(month_offset)
        if direct_value is not None:
            return direct_value

        target_month = _shift_month(bill.bill_month, month_offset)
        for point in bill.history:
            if point.month == target_month:
                return point.usage_kwh
        return None

    return value


def _history_amount(bill: KepcoBill, month: str) -> int | None:
    """Return the billed amount recorded for a history month when the response carried one."""
    for point in bill.history:
        if point.month == month:
            return point.amount_krw
    return None


def _household_usage(bill: KepcoBill, options: dict[str, Any]) -> int | None:
    """Return household usage, deriving it only when the response omits one side."""
    del options
    if bill.household_usage_kwh is not None:
        return bill.household_usage_kwh
    if bill.usage_kwh is None:
        return None
    if bill.common_usage_kwh is None:
        return bill.usage_kwh
    derived = bill.usage_kwh - bill.common_usage_kwh
    return derived if derived >= 0 else None


def _common_usage(bill: KepcoBill, options: dict[str, Any]) -> int | None:
    """Return common-area usage, deriving it only when the response omits one side."""
    del options
    if bill.common_usage_kwh is not None:
        return bill.common_usage_kwh
    if bill.usage_kwh is None:
        return None
    if bill.household_usage_kwh is None:
        return 0
    derived = bill.usage_kwh - bill.household_usage_kwh
    return derived if derived >= 0 else None


def _co2_estimate(field: str) -> KepcoValueFunction:
    """Return a user-coefficient CO2 estimate for one usage field."""

    def value(bill: KepcoBill, options: dict[str, Any]) -> int | None:
        usage = getattr(bill, field)
        if not isinstance(usage, int):
            return None
        factor = options.get(OPT_CO2_FACTOR_KG_PER_KWH, DEFAULT_CO2_FACTOR_KG_PER_KWH)
        try:
            decimal_factor = Decimal(str(factor))
            if not decimal_factor.is_finite() or not 0 < decimal_factor <= 10:
                return None
            estimate = Decimal(usage) * decimal_factor
        except InvalidOperation, ValueError:
            return None
        return round(estimate)

    return value


MONTHLY_USAGE_SENSOR_DESCRIPTIONS: tuple[KepcoSensorEntityDescription, ...] = (
    KepcoSensorEntityDescription(
        key="usage_history_last_year_two_months_ago",
        translation_key="monthly_usage_period",
        device_group=KepcoDeviceGroup.MONTHLY_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_history_usage(-14),
        history_month_offset=-14,
    ),
    KepcoSensorEntityDescription(
        key="usage_history_two_months_ago",
        translation_key="monthly_usage_period",
        device_group=KepcoDeviceGroup.MONTHLY_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_history_usage(-2),
        history_month_offset=-2,
    ),
    KepcoSensorEntityDescription(
        key="usage_history_last_year_previous_month",
        translation_key="monthly_usage_period",
        device_group=KepcoDeviceGroup.MONTHLY_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_history_usage(-13),
        history_month_offset=-13,
    ),
    KepcoSensorEntityDescription(
        key="usage_history_previous_month",
        translation_key="monthly_usage_period",
        device_group=KepcoDeviceGroup.MONTHLY_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_history_usage(-1),
        history_month_offset=-1,
    ),
    KepcoSensorEntityDescription(
        key="usage_history_last_year_same_month",
        translation_key="monthly_usage_period",
        device_group=KepcoDeviceGroup.MONTHLY_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_history_usage(-12),
        history_month_offset=-12,
    ),
    KepcoSensorEntityDescription(
        key="usage_history_current_month",
        translation_key="monthly_usage_period",
        device_group=KepcoDeviceGroup.MONTHLY_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_history_usage(0),
        history_month_offset=0,
    ),
)

METER_USAGE_SENSOR_DESCRIPTIONS: tuple[KepcoSensorEntityDescription, ...] = (
    KepcoSensorEntityDescription(
        key="usage_period_start",
        translation_key="usage_period_start",
        device_group=KepcoDeviceGroup.METER_USAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.DATE,
        value_fn=_usage("period_start"),
    ),
    KepcoSensorEntityDescription(
        key="usage_period_end",
        translation_key="usage_period_end",
        device_group=KepcoDeviceGroup.METER_USAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.DATE,
        value_fn=_usage("period_end"),
    ),
    KepcoSensorEntityDescription(
        key="meter_reading_day",
        translation_key="meter_reading_day",
        device_group=KepcoDeviceGroup.METER_USAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_usage("meter_reading_day"),
    ),
    KepcoSensorEntityDescription(
        key="meter_reading",
        translation_key="meter_reading",
        device_group=KepcoDeviceGroup.METER_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_usage("current_meter_reading"),
    ),
    KepcoSensorEntityDescription(
        key="previous_meter_reading",
        translation_key="previous_meter_reading",
        device_group=KepcoDeviceGroup.METER_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_usage("previous_meter_reading"),
    ),
    KepcoSensorEntityDescription(
        key="monthly_usage",
        translation_key="monthly_usage",
        device_group=KepcoDeviceGroup.METER_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_usage("usage_kwh"),
    ),
    KepcoSensorEntityDescription(
        key="household_usage",
        translation_key="household_usage",
        device_group=KepcoDeviceGroup.METER_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_household_usage,
    ),
    KepcoSensorEntityDescription(
        key="common_usage",
        translation_key="common_usage",
        device_group=KepcoDeviceGroup.METER_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_common_usage,
    ),
    KepcoSensorEntityDescription(
        key="previous_month_usage",
        translation_key="previous_month_usage",
        device_group=KepcoDeviceGroup.METER_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_usage("previous_usage_kwh"),
    ),
    KepcoSensorEntityDescription(
        key="last_year_same_month_usage",
        translation_key="last_year_same_month_usage",
        device_group=KepcoDeviceGroup.METER_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_usage("last_year_usage_kwh"),
    ),
    KepcoSensorEntityDescription(
        key="current_period_usage",
        translation_key="current_period_usage",
        suggested_display_precision=2,
        device_group=KepcoDeviceGroup.METER_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda bill, options: bill.current_period_usage_kwh,
    ),
    KepcoSensorEntityDescription(
        key="predicted_period_usage",
        translation_key="predicted_period_usage",
        suggested_display_precision=2,
        device_group=KepcoDeviceGroup.METER_USAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda bill, options: bill.predicted_period_usage_kwh,
    ),
)

ELECTRICITY_CHARGE_SENSOR_DESCRIPTIONS: tuple[KepcoSensorEntityDescription, ...] = (
    KepcoSensorEntityDescription(
        key="electricity_subtotal",
        translation_key="electricity_subtotal",
        device_group=KepcoDeviceGroup.ELECTRICITY_CHARGE,
        native_unit_of_measurement="KRW",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_charge("subtotal_krw"),
    ),
    KepcoSensorEntityDescription(
        key="base_charge",
        translation_key="base_charge",
        device_group=KepcoDeviceGroup.ELECTRICITY_CHARGE,
        native_unit_of_measurement="KRW",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_charge("base_krw"),
    ),
    KepcoSensorEntityDescription(
        key="energy_charge",
        translation_key="energy_charge",
        device_group=KepcoDeviceGroup.ELECTRICITY_CHARGE,
        native_unit_of_measurement="KRW",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_charge("energy_krw"),
    ),
    KepcoSensorEntityDescription(
        key="climate_environment_charge",
        translation_key="climate_environment_charge",
        device_group=KepcoDeviceGroup.ELECTRICITY_CHARGE,
        native_unit_of_measurement="KRW",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_charge("climate_krw"),
    ),
    KepcoSensorEntityDescription(
        key="fuel_adjustment_charge",
        translation_key="fuel_adjustment_charge",
        device_group=KepcoDeviceGroup.ELECTRICITY_CHARGE,
        native_unit_of_measurement="KRW",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_charge("fuel_krw"),
    ),
    KepcoSensorEntityDescription(
        key="child_discount",
        translation_key="child_discount",
        device_group=KepcoDeviceGroup.ELECTRICITY_CHARGE,
        native_unit_of_measurement="KRW",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_charge("child_discount_krw"),
    ),
    KepcoSensorEntityDescription(
        key="vat",
        translation_key="vat",
        device_group=KepcoDeviceGroup.ELECTRICITY_CHARGE,
        native_unit_of_measurement="KRW",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_charge("vat_krw"),
    ),
    KepcoSensorEntityDescription(
        key="power_industry_fund",
        translation_key="power_industry_fund",
        device_group=KepcoDeviceGroup.ELECTRICITY_CHARGE,
        native_unit_of_measurement="KRW",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_charge("fund_krw"),
    ),
    KepcoSensorEntityDescription(
        key="rounding_amount",
        translation_key="rounding_amount",
        device_group=KepcoDeviceGroup.ELECTRICITY_CHARGE,
        native_unit_of_measurement="KRW",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_charge("rounding_krw"),
    ),
    KepcoSensorEntityDescription(
        key="amount_due",
        translation_key="amount_due",
        device_group=KepcoDeviceGroup.ELECTRICITY_CHARGE,
        native_unit_of_measurement="KRW",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_usage("amount_krw"),
    ),
)

NEIGHBOR_COMPARISON_SENSOR_DESCRIPTIONS: tuple[KepcoSensorEntityDescription, ...] = (
    KepcoSensorEntityDescription(
        key="neighbor_usage_comparison",
        translation_key="customer_usage",
        device_group=KepcoDeviceGroup.NEIGHBOR_COMPARISON,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_usage("usage_kwh"),
    ),
    KepcoSensorEntityDescription(
        key="building_average_usage",
        translation_key="same_building_usage",
        device_group=KepcoDeviceGroup.NEIGHBOR_COMPARISON,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_usage("building_average_kwh"),
    ),
    KepcoSensorEntityDescription(
        key="apartment_average_usage",
        translation_key="apartment_total_usage",
        device_group=KepcoDeviceGroup.NEIGHBOR_COMPARISON,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=_usage("apartment_average_kwh"),
    ),
)

GREENHOUSE_GAS_SENSOR_DESCRIPTIONS: tuple[KepcoSensorEntityDescription, ...] = (
    KepcoSensorEntityDescription(
        key="co2_estimate",
        translation_key="current_month_co2",
        device_group=KepcoDeviceGroup.GREENHOUSE_GAS,
        native_unit_of_measurement="kg CO₂",
        value_fn=_co2_estimate("usage_kwh"),
    ),
    KepcoSensorEntityDescription(
        key="previous_month_co2_estimate",
        translation_key="previous_month_co2",
        device_group=KepcoDeviceGroup.GREENHOUSE_GAS,
        native_unit_of_measurement="kg CO₂",
        value_fn=_co2_estimate("previous_usage_kwh"),
    ),
    KepcoSensorEntityDescription(
        key="last_year_same_month_co2_estimate",
        translation_key="last_year_same_month_co2",
        device_group=KepcoDeviceGroup.GREENHOUSE_GAS,
        native_unit_of_measurement="kg CO₂",
        value_fn=_co2_estimate("last_year_usage_kwh"),
    ),
)

SENSOR_DESCRIPTIONS = (
    *MONTHLY_USAGE_SENSOR_DESCRIPTIONS,
    *METER_USAGE_SENSOR_DESCRIPTIONS,
    *ELECTRICITY_CHARGE_SENSOR_DESCRIPTIONS,
    *NEIGHBOR_COMPARISON_SENSOR_DESCRIPTIONS,
    *GREENHOUSE_GAS_SENSOR_DESCRIPTIONS,
)
ACTIVE_SENSOR_KEYS = frozenset(description.key for description in SENSOR_DESCRIPTIONS)
REMOVED_SENSOR_KEYS = frozenset({"billing_month"})
KNOWN_SENSOR_KEYS = tuple(
    sorted((*ACTIVE_SENSOR_KEYS, *REMOVED_SENSOR_KEYS), key=len, reverse=True)
)


def _device_identifier(customer: KepcoCustomer, group: KepcoDeviceGroup) -> str:
    """Return a stable device identifier while preserving the legacy primary device."""
    if group is KepcoDeviceGroup.METER_USAGE:
        return customer.stable_key
    return f"{customer.stable_key}:{group.value}"


def _device_info(customer: KepcoCustomer, group: KepcoDeviceGroup) -> DeviceInfo:
    """Return one privacy-safe logical device description."""
    group_name = _DEVICE_GROUP_NAMES[group]
    return {
        "identifiers": {(DOMAIN, _device_identifier(customer, group))},
        "name": group_name,
        "manufacturer": "한국전력공사(KEPCO)",
        "model": "한전ON",
        "configuration_url": PAGE_URL,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KepcoOnConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up five logical KEPCO ON devices and their bill sensors."""
    coordinator = entry.runtime_data.coordinator
    customers = coordinator.data.customers
    await _async_remove_stale_registry_entries(hass, entry, customers)
    options = dict(entry.options)

    entities = [
        KepcoOnSensor(coordinator, customer, description, options)
        for customer in customers
        for description in SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


class KepcoOnSensor(CoordinatorEntity[KepcoOnDataUpdateCoordinator], SensorEntity):
    """Sensor backed by one selected customer's coordinator bill snapshot."""

    _attr_has_entity_name = True
    entity_description: KepcoSensorEntityDescription

    def __init__(
        self,
        coordinator: KepcoOnDataUpdateCoordinator,
        customer: KepcoCustomer,
        description: KepcoSensorEntityDescription,
        options: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self.customer = customer
        self.entity_description = description
        self._options = options
        self._attr_unique_id = f"{customer.stable_key}_{description.key}"
        self._attr_device_info = _device_info(customer, description.device_group)
        self._refresh_month_translation_placeholders()

    def _bill(self) -> KepcoBill | None:
        """Return this customer's current bill snapshot."""
        return self.coordinator.data.bills_by_customer_key.get(self.customer.stable_key)

    def _refresh_month_translation_placeholders(self) -> None:
        """Keep relative history entity names aligned to the latest billing month."""
        offset = self.entity_description.history_month_offset
        if offset is None:
            return
        bill = self._bill()
        target_month = _shift_month(bill.bill_month, offset) if bill is not None else "000000"
        placeholders = {
            "year": target_month[:4] if target_month != "000000" else "----",
            "month": str(int(target_month[4:])) if target_month != "000000" else "--",
        }
        if getattr(self, "_attr_translation_placeholders", None) == placeholders:
            return
        self._attr_translation_placeholders = placeholders
        self.__dict__.pop("translation_placeholders", None)
        self.__dict__.pop("name", None)

    def _handle_coordinator_update(self) -> None:
        """Refresh dynamic month labels before writing the new state."""
        self._refresh_month_translation_placeholders()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return if the customer bill is available."""
        if not super().available:
            return False
        # A valid bill snapshot can have missing optional planner values.
        # Keep those states unknown so Home Assistant publishes their diagnostics;
        # unavailable entities lose extra_state_attributes in Entity state writing.
        data = self.coordinator.data
        return (
            self.customer.stable_key in data.bills_by_customer_key
            and self.customer.stable_key not in data.errors_by_customer_key
        )

    @property
    def native_value(self) -> KepcoSensorValue:
        """Return the current native sensor value."""
        bill = self._bill()
        if bill is None or self.customer.stable_key in self.coordinator.data.errors_by_customer_key:
            return None
        return self.entity_description.value_fn(bill, self._options)

    @property
    def extra_state_attributes(self) -> KepcoSensorAttributes:
        """Return non-sensitive billing context attributes."""
        bill = self._bill()
        if bill is None:
            return {}
        key = self.entity_description.key
        if key in POWER_PLANNER_FIELDS:
            combined = self.customer.contract_method == COMBINED_APARTMENT_PLANNER_CONTRACT
            field_status = (
                bill.power_planner_current_status
                if key == "current_period_usage"
                else bill.power_planner_prediction_status
            )
            status = field_status or bill.power_planner_status
            if (
                field_status is None
                and key == "predicted_period_usage"
                and (
                    status == "ok"
                    or (status == "no_data" and bill.power_planner_return_code == "00")
                )
            ):
                status = "no_data" if combined else "source_unit_unverified"
            # Billing dates describe a past bill, not the current planner period.
            return {
                "data_source": "kepco_power_planner",
                "source_field": POWER_PLANNER_FIELDS[key],
                "data_status": status,
                "data_status_message": POWER_PLANNER_MESSAGES.get(status, status),
                "return_code": bill.power_planner_return_code,
                "provider_return_code": bill.power_planner_return_code,
                "integration_version": VERSION,
                "request_variant": (
                    "apartment_customer_and_contract" if combined else "household_and_change_date"
                ),
                "value_divisor": 1000
                if combined
                else (1 if key == "current_period_usage" else None),
                "conversion_basis": (
                    "user_reported_combined_contract" if combined else "legacy_contract_profile"
                ),
            }
        offset = self.entity_description.history_month_offset
        billing_month = (
            _shift_month(bill.bill_month, offset) if offset is not None else bill.bill_month
        )
        attributes: KepcoSensorAttributes = {"billing_month": billing_month}
        if offset is None:
            if bill.period_start is not None:
                attributes["usage_period_start"] = bill.period_start
            if bill.period_end is not None:
                attributes["usage_period_end"] = bill.period_end
        else:
            amount = _history_amount(bill, billing_month)
            if amount is not None:
                attributes["amount_krw"] = amount
        return attributes


async def _async_remove_stale_registry_entries(
    hass: HomeAssistant,
    entry: KepcoOnConfigEntry,
    customers: Iterable[KepcoCustomer],
) -> None:
    """Remove stale entries and migrate integration-disabled legacy entities."""
    selected_keys = {customer.stable_key for customer in customers}
    entity_registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if not _is_this_entry_domain_entity(registry_entry, entry.entry_id):
            continue
        parsed = _sensor_identity_from_unique_id(registry_entry.unique_id)
        if parsed is None:
            continue
        customer_key, sensor_key = parsed
        if customer_key not in selected_keys or sensor_key in REMOVED_SENSOR_KEYS:
            entity_registry.async_remove(registry_entry.entity_id)
            continue
        if (
            sensor_key in ACTIVE_SENSOR_KEYS
            and registry_entry.disabled_by == RegistryEntryDisabler.INTEGRATION
        ):
            entity_registry.async_update_entity(registry_entry.entity_id, disabled_by=None)

    device_registry = dr.async_get(hass)
    for device_entry in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        device_customer_key = _customer_key_from_device_identifiers(device_entry.identifiers)
        if device_customer_key is None or device_customer_key in selected_keys:
            continue
        if set(device_entry.config_entries) == {entry.entry_id}:
            device_registry.async_remove_device(device_entry.id)


def _sensor_identity_from_unique_id(unique_id: str) -> tuple[str, str] | None:
    for sensor_key in KNOWN_SENSOR_KEYS:
        suffix = f"_{sensor_key}"
        if unique_id.endswith(suffix):
            return unique_id[: -len(suffix)], sensor_key
    return None


def _is_this_entry_domain_entity(registry_entry: RegistryEntry, entry_id: str) -> bool:
    return registry_entry.platform == DOMAIN and registry_entry.config_entry_id == entry_id


def _customer_key_from_device_identifiers(identifiers: Iterable[tuple[str, ...]]) -> str | None:
    group_suffixes = tuple(
        f":{group.value}" for group in KepcoDeviceGroup if group is not KepcoDeviceGroup.METER_USAGE
    )
    for identifier_tuple in identifiers:
        if len(identifier_tuple) != 2:
            continue
        domain, identifier = identifier_tuple
        if domain != DOMAIN:
            continue
        for suffix in group_suffixes:
            if identifier.endswith(suffix):
                return identifier[: -len(suffix)]
        return identifier
    return None


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: KepcoOnConfigEntry,
    device_entry: DeviceEntry,
) -> bool:
    """Return whether Home Assistant may remove a stale KEPCO ON logical device."""
    del hass
    selected_keys = {
        customer.stable_key for customer in config_entry.runtime_data.coordinator.data.customers
    }
    customer_key = _customer_key_from_device_identifiers(device_entry.identifiers)
    return customer_key is not None and customer_key not in selected_keys


__all__ = [
    "KepcoDeviceGroup",
    "async_remove_config_entry_device",
    "async_setup_entry",
]
