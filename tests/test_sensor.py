"""Sensor platform contract tests for KEPCO ON."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from custom_components.kepco_on.const import (
    DEFAULT_CO2_FACTOR_KG_PER_KWH,
    DOMAIN,
    OPT_CO2_FACTOR_KG_PER_KWH,
    PAGE_URL,
)
from custom_components.kepco_on.models import (
    KepcoBill,
    KepcoChargeBreakdown,
    KepcoCoordinatorData,
    KepcoCustomer,
    KepcoUsageHistoryPoint,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.helpers.entity_registry import RegistryEntryDisabler

ROOT = Path(__file__).resolve().parents[1]
RAW_CUSTOMER_SECRET = "RAW_CUST_1234567890"
RAW_HOUSE_SECRET = "RAW_HOUSE_0987654321"
RAW_NAME_SECRET = "홍길동"


@dataclass(slots=True)
class FakeCoordinator:
    """Minimal coordinator surface read by CoordinatorEntity sensors."""

    data: KepcoCoordinatorData
    last_update_success: bool = True
    refresh_calls: int = 0
    listener_calls: list[object] | None = None

    def __post_init__(self) -> None:
        self.listener_calls = []

    def async_add_listener(self, listener: object, context: object = None) -> object:
        assert self.listener_calls is not None
        self.listener_calls.append((listener, context))

        def remove_listener() -> None:
            return None

        return remove_listener

    async def async_request_refresh(self) -> None:
        self.refresh_calls += 1


class FakeEntityRegistry:
    """Entity registry fake for stale cleanup assertions."""

    def __init__(self, entries: list[SimpleNamespace]) -> None:
        self.entries = entries
        self.removed: list[str] = []
        self.updated: list[tuple[str, RegistryEntryDisabler | None]] = []

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)

    def async_update_entity(
        self,
        entity_id: str,
        *,
        disabled_by: RegistryEntryDisabler | None,
    ) -> None:
        self.updated.append((entity_id, disabled_by))


class FakeDeviceRegistry:
    """Device registry fake for stale cleanup assertions."""

    def __init__(self, entries: list[SimpleNamespace]) -> None:
        self.entries = entries
        self.removed: list[str] = []

    def async_remove_device(self, device_id: str) -> None:
        self.removed.append(device_id)


class FakeHass:
    """Hashable HA stand-in for registry singleton helpers."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}


def customer(stable_key: str, *, apartment: str = "푸른아파트") -> KepcoCustomer:
    """Return a privacy-canary customer."""
    return KepcoCustomer(
        stable_key=stable_key,
        apartment_name=apartment,
        dong="101",
        ho="1001",
        contract_method="아파트(단일계약)",
        is_supported=True,
        _customer_number=f"{RAW_CUSTOMER_SECRET}_{stable_key}",
        _house_contract_number=f"{RAW_HOUSE_SECRET}_{stable_key}",
    )


def usage_history(*, through_month: str = "202608") -> tuple[KepcoUsageHistoryPoint, ...]:
    """Return the six historical points used by grouped history sensors."""
    values = {
        "202506": 399,
        "202507": 459,
        "202508": 612,
        "202606": 371,
        "202607": 406,
        "202608": 573,
        "202509": 576,
        "202609": 610,
    }
    return tuple(
        KepcoUsageHistoryPoint(month=month, usage_kwh=value)
        for month, value in sorted(values.items())
        if month <= through_month
    )


def bill(
    *,
    bill_month: str = "202608",
    usage_kwh: int | None = 573,
    household_usage_kwh: int | None = None,
    common_usage_kwh: int | None = None,
    previous_usage_kwh: int | None = 406,
    last_year_usage_kwh: int | None = 612,
    amount_krw: int | None = 96330,
    child_discount_krw: int | None = -16000,
    building_average_kwh: int | None = 363,
    apartment_average_kwh: int | None = 284,
    history: tuple[KepcoUsageHistoryPoint, ...] | None = None,
) -> KepcoBill:
    """Return a synthetic bill with every grouped sensor field populated."""
    return KepcoBill(
        bill_month=bill_month,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        usage_kwh=usage_kwh,
        household_usage_kwh=household_usage_kwh,
        common_usage_kwh=common_usage_kwh,
        previous_usage_kwh=previous_usage_kwh,
        last_year_usage_kwh=last_year_usage_kwh,
        building_average_kwh=building_average_kwh,
        apartment_average_kwh=apartment_average_kwh,
        current_meter_reading=23139,
        previous_meter_reading=22566,
        meter_reading_day="01",
        amount_krw=amount_krw,
        charge=KepcoChargeBreakdown(
            subtotal_krw=85484,
            base_krw=6060,
            energy_krw=87402,
            climate_krw=5157,
            fuel_krw=2865,
            child_discount_krw=child_discount_krw,
            vat_krw=8548,
            fund_krw=2300,
            rounding_krw=2,
        ),
        history=history if history is not None else usage_history(through_month=bill_month),
    )


def entry(
    *,
    coordinator: FakeCoordinator,
    options: dict[str, Any] | None = None,
    entry_id: str = "entry-1",
) -> SimpleNamespace:
    """Return a typed-runtime config-entry stand-in."""
    return SimpleNamespace(
        entry_id=entry_id,
        options=options or {},
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )


async def setup_entities(
    *,
    customers: tuple[KepcoCustomer, ...] = (customer("cust-a"),),
    bills_by_customer_key: dict[str, KepcoBill] | None = None,
    errors_by_customer_key: dict[str, str] | None = None,
    options: dict[str, Any] | None = None,
    use_real_registry_cleanup: bool = False,
) -> list[Any]:
    """Set up sensor entities and return the collected entity list."""
    from custom_components.kepco_on import sensor as sensor_module

    data = KepcoCoordinatorData(
        customers=customers,
        bills_by_customer_key=bills_by_customer_key
        if bills_by_customer_key is not None
        else {customers[0].stable_key: bill()},
        errors_by_customer_key=errors_by_customer_key or {},
    )
    coordinator = FakeCoordinator(data)
    config_entry = entry(coordinator=coordinator, options=options)
    entities: list[Any] = []

    sensor_any = cast("Any", sensor_module)
    add_entities = cast("Any", entities.extend)
    if use_real_registry_cleanup:
        await sensor_module.async_setup_entry(
            cast("Any", FakeHass()), cast("Any", config_entry), add_entities
        )
        return entities

    entity_registry = FakeEntityRegistry([])
    device_registry = FakeDeviceRegistry([])
    original_entity_get = sensor_any.er.async_get
    original_entity_entries = sensor_any.er.async_entries_for_config_entry
    original_device_get = sensor_any.dr.async_get
    original_device_entries = sensor_any.dr.async_entries_for_config_entry
    sensor_any.er.async_get = lambda hass: entity_registry
    sensor_any.er.async_entries_for_config_entry = lambda reg, entry_id: reg.entries
    sensor_any.dr.async_get = lambda hass: device_registry
    sensor_any.dr.async_entries_for_config_entry = lambda reg, entry_id: reg.entries
    try:
        await sensor_module.async_setup_entry(
            cast("Any", FakeHass()), cast("Any", config_entry), add_entities
        )
    finally:
        sensor_any.er.async_get = original_entity_get
        sensor_any.er.async_entries_for_config_entry = original_entity_entries
        sensor_any.dr.async_get = original_device_get
        sensor_any.dr.async_entries_for_config_entry = original_device_entries

    return entities


def entity_entry(
    entity_id: str,
    unique_id: str,
    *,
    platform: str = DOMAIN,
    config_entry_id: str = "entry-1",
    disabled_by: RegistryEntryDisabler | None = None,
) -> SimpleNamespace:
    """Return a registry entry fake."""
    return SimpleNamespace(
        entity_id=entity_id,
        unique_id=unique_id,
        platform=platform,
        config_entry_id=config_entry_id,
        disabled_by=disabled_by,
    )


def device_entry(
    device_id: str,
    identifiers: set[tuple[str, ...]],
    *,
    config_entries: set[str] | None = None,
) -> SimpleNamespace:
    """Return a device registry entry fake."""
    return SimpleNamespace(
        id=device_id,
        identifiers=identifiers,
        config_entries=config_entries or {"entry-1"},
    )


def by_key(entities: list[Any]) -> dict[str, Any]:
    """Return entities keyed by sensor description key."""
    return {entity.entity_description.key: entity for entity in entities}


@pytest.mark.asyncio
async def test_sensors_have_exact_five_device_groups_counts_values_and_privacy() -> None:
    from custom_components.kepco_on.sensor import KepcoDeviceGroup

    entities = await setup_entities()
    sensors = by_key(entities)

    assert len(entities) == 34
    assert len(sensors) == 34
    assert {
        group: sum(entity.entity_description.device_group is group for entity in entities)
        for group in KepcoDeviceGroup
    } == {
        KepcoDeviceGroup.MONTHLY_USAGE: 6,
        KepcoDeviceGroup.METER_USAGE: 12,
        KepcoDeviceGroup.ELECTRICITY_CHARGE: 10,
        KepcoDeviceGroup.NEIGHBOR_COMPARISON: 3,
        KepcoDeviceGroup.GREENHOUSE_GAS: 3,
    }

    device_info_by_group = {
        entity.entity_description.device_group: entity.device_info for entity in entities
    }
    assert device_info_by_group == {
        KepcoDeviceGroup.MONTHLY_USAGE: {
            "identifiers": {(DOMAIN, "cust-a:monthly_usage")},
            "name": "월별 사용량",
            "manufacturer": "한국전력공사(KEPCO)",
            "model": "한전ON",
            "configuration_url": PAGE_URL,
        },
        KepcoDeviceGroup.METER_USAGE: {
            "identifiers": {(DOMAIN, "cust-a")},
            "name": "검침/전기사용량",
            "manufacturer": "한국전력공사(KEPCO)",
            "model": "한전ON",
            "configuration_url": PAGE_URL,
        },
        KepcoDeviceGroup.ELECTRICITY_CHARGE: {
            "identifiers": {(DOMAIN, "cust-a:electricity_charge")},
            "name": "전기요금",
            "manufacturer": "한국전력공사(KEPCO)",
            "model": "한전ON",
            "configuration_url": PAGE_URL,
        },
        KepcoDeviceGroup.NEIGHBOR_COMPARISON: {
            "identifiers": {(DOMAIN, "cust-a:neighbor_comparison")},
            "name": "이웃 전기사용량 비교",
            "manufacturer": "한국전력공사(KEPCO)",
            "model": "한전ON",
            "configuration_url": PAGE_URL,
        },
        KepcoDeviceGroup.GREENHOUSE_GAS: {
            "identifiers": {(DOMAIN, "cust-a:greenhouse_gas")},
            "name": "온실가스 배출량",
            "manufacturer": "한국전력공사(KEPCO)",
            "model": "한전ON",
            "configuration_url": PAGE_URL,
        },
    }

    for key, entity in sensors.items():
        assert entity.has_entity_name is True
        assert entity.unique_id == f"cust-a_{key}"
        assert entity.entity_description.entity_registry_enabled_default is True
        rendered = repr(entity.device_info) + repr(entity.unique_id) + repr(entity.native_value)
        assert RAW_CUSTOMER_SECRET not in rendered
        assert RAW_HOUSE_SECRET not in rendered
        assert RAW_NAME_SECRET not in rendered

    diagnostic_keys = {
        "usage_period_start",
        "usage_period_end",
        "meter_reading_day",
    }
    assert {
        entity.entity_description.key
        for entity in entities
        if entity.entity_description.entity_category is EntityCategory.DIAGNOSTIC
    } == diagnostic_keys
    assert all(
        entity.entity_description.entity_category is None
        for entity in entities
        if entity.entity_description.key not in diagnostic_keys
    )

    history_values = {
        "usage_history_last_year_two_months_ago": (399, {"year": "2025", "month": "6"}),
        "usage_history_two_months_ago": (371, {"year": "2026", "month": "6"}),
        "usage_history_last_year_previous_month": (459, {"year": "2025", "month": "7"}),
        "usage_history_previous_month": (406, {"year": "2026", "month": "7"}),
        "usage_history_last_year_same_month": (612, {"year": "2025", "month": "8"}),
        "usage_history_current_month": (573, {"year": "2026", "month": "8"}),
    }
    for key, (value, placeholders) in history_values.items():
        entity = sensors[key]
        assert entity.native_value == value
        assert entity.translation_key == "monthly_usage_period"
        assert entity.translation_placeholders == placeholders
        assert entity.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
        assert entity.device_class == SensorDeviceClass.ENERGY
        assert entity.state_class is None
        assert entity.extra_state_attributes == {
            "billing_month": f"{placeholders['year']}{int(placeholders['month']):02d}"
        }

    assert sensors["usage_period_start"].native_value == date(2026, 7, 1)
    assert sensors["usage_period_start"].device_class == SensorDeviceClass.DATE
    assert sensors["usage_period_end"].native_value == date(2026, 7, 31)
    assert sensors["usage_period_end"].device_class == SensorDeviceClass.DATE
    assert sensors["meter_reading_day"].native_value == "01"
    assert sensors["meter_reading"].native_value == 23139
    assert sensors["meter_reading"].state_class == SensorStateClass.TOTAL_INCREASING
    assert sensors["previous_meter_reading"].native_value == 22566
    assert sensors["monthly_usage"].native_value == 573
    assert sensors["household_usage"].native_value == 573
    assert sensors["common_usage"].native_value == 0
    assert sensors["previous_month_usage"].native_value == 406
    assert sensors["last_year_same_month_usage"].native_value == 612
    assert sensors["monthly_usage"].extra_state_attributes == {
        "billing_month": "202608",
        "usage_period_start": date(2026, 7, 1),
        "usage_period_end": date(2026, 7, 31),
    }

    charge_values = {
        "electricity_subtotal": 85484,
        "base_charge": 6060,
        "energy_charge": 87402,
        "climate_environment_charge": 5157,
        "fuel_adjustment_charge": 2865,
        "child_discount": -16000,
        "vat": 8548,
        "power_industry_fund": 2300,
        "rounding_amount": 2,
        "amount_due": 96330,
    }
    for key, value in charge_values.items():
        assert sensors[key].native_value == value
        assert sensors[key].native_unit_of_measurement == "KRW"
        assert sensors[key].device_class == SensorDeviceClass.MONETARY
        assert sensors[key].state_class is None

    assert sensors["neighbor_usage_comparison"].native_value == 573
    assert sensors["neighbor_usage_comparison"].translation_key == "customer_usage"
    assert sensors["neighbor_usage_comparison"].extra_state_attributes == {
        "billing_month": "202608",
        "usage_period_start": date(2026, 7, 1),
        "usage_period_end": date(2026, 7, 31),
    }
    assert sensors["building_average_usage"].native_value == 363
    assert sensors["building_average_usage"].translation_key == "same_building_usage"
    assert sensors["apartment_average_usage"].native_value == 284
    assert sensors["apartment_average_usage"].translation_key == "apartment_total_usage"

    assert sensors["co2_estimate"].native_value == 263
    assert sensors["previous_month_co2_estimate"].native_value == 186
    assert sensors["last_year_same_month_co2_estimate"].native_value == 281
    for key in (
        "co2_estimate",
        "previous_month_co2_estimate",
        "last_year_same_month_co2_estimate",
    ):
        assert sensors[key].native_unit_of_measurement == "kg CO₂"
        assert sensors[key].device_class is None
        assert sensors[key].state_class is None


@pytest.mark.asyncio
async def test_history_uses_direct_values_then_history_and_handles_missing_points() -> None:
    synthetic_history = (
        KepcoUsageHistoryPoint("202506", 399),
        KepcoUsageHistoryPoint("202507", 459),
        KepcoUsageHistoryPoint("202508", 612),
        KepcoUsageHistoryPoint("202606", 371),
        KepcoUsageHistoryPoint("202607", 407),
        KepcoUsageHistoryPoint("202608", 574),
    )
    sensors = by_key(
        await setup_entities(
            bills_by_customer_key={
                "cust-a": bill(
                    usage_kwh=573,
                    previous_usage_kwh=406,
                    last_year_usage_kwh=611,
                    history=synthetic_history,
                )
            }
        )
    )

    assert sensors["usage_history_current_month"].native_value == 573
    assert sensors["usage_history_previous_month"].native_value == 406
    assert sensors["usage_history_last_year_same_month"].native_value == 611
    assert sensors["usage_history_two_months_ago"].native_value == 371

    missing = by_key(
        await setup_entities(
            bills_by_customer_key={
                "cust-a": bill(
                    usage_kwh=None,
                    previous_usage_kwh=None,
                    last_year_usage_kwh=None,
                    history=(KepcoUsageHistoryPoint("202608", 575),),
                )
            }
        )
    )
    assert missing["usage_history_current_month"].native_value == 575
    assert missing["usage_history_previous_month"].native_value is None
    assert missing["usage_history_last_year_two_months_ago"].native_value is None


@pytest.mark.asyncio
async def test_history_placeholders_refresh_when_billing_month_changes() -> None:
    entities = await setup_entities()
    sensors = by_key(entities)
    target = sensors["usage_history_current_month"]
    assert target.translation_placeholders == {"year": "2026", "month": "8"}

    target.coordinator.data = KepcoCoordinatorData(
        customers=(customer("cust-a"),),
        bills_by_customer_key={
            "cust-a": bill(
                bill_month="202609",
                usage_kwh=610,
                previous_usage_kwh=573,
                last_year_usage_kwh=576,
                history=usage_history(through_month="202609"),
            )
        },
    )
    target.async_write_ha_state = lambda: None
    target._handle_coordinator_update()

    assert target.translation_placeholders == {"year": "2026", "month": "9"}
    assert target.native_value == 610
    assert target.extra_state_attributes == {"billing_month": "202609"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "usage",
        "household",
        "common",
        "expected_household",
        "expected_common",
    ),
    [
        (573, 560, 13, 560, 13),
        (573, None, None, 573, 0),
        (573, None, 13, 560, 13),
        (573, 560, None, 560, 13),
        (573, None, 600, None, 600),
        (573, 600, None, 600, None),
        (None, None, None, None, None),
    ],
)
async def test_household_and_common_usage_derivation(
    usage: int | None,
    household: int | None,
    common: int | None,
    expected_household: int | None,
    expected_common: int | None,
) -> None:
    sensors = by_key(
        await setup_entities(
            bills_by_customer_key={
                "cust-a": bill(
                    usage_kwh=usage,
                    household_usage_kwh=household,
                    common_usage_kwh=common,
                )
            }
        )
    )

    assert sensors["household_usage"].native_value == expected_household
    assert sensors["common_usage"].native_value == expected_common


@pytest.mark.asyncio
async def test_co2_sensors_use_default_custom_and_invalid_factors() -> None:
    default_sensors = by_key(await setup_entities())
    assert DEFAULT_CO2_FACTOR_KG_PER_KWH == 0.459
    assert default_sensors["co2_estimate"].native_value == 263
    assert default_sensors["previous_month_co2_estimate"].native_value == 186
    assert default_sensors["last_year_same_month_co2_estimate"].native_value == 281

    custom = by_key(await setup_entities(options={OPT_CO2_FACTOR_KG_PER_KWH: "0.5"}))
    assert custom["co2_estimate"].native_value == 286
    assert custom["previous_month_co2_estimate"].native_value == 203
    assert custom["last_year_same_month_co2_estimate"].native_value == 306

    for factor in ("bad", 0, -1, "NaN", "Infinity", "-Infinity", 10.1):
        invalid = by_key(await setup_entities(options={OPT_CO2_FACTOR_KG_PER_KWH: factor}))
        assert invalid["co2_estimate"].native_value is None
        assert invalid["previous_month_co2_estimate"].native_value is None
        assert invalid["last_year_same_month_co2_estimate"].native_value is None

    missing_usage = by_key(
        await setup_entities(
            bills_by_customer_key={"cust-a": bill(usage_kwh=None)},
        )
    )
    assert missing_usage["co2_estimate"].native_value is None


@pytest.mark.asyncio
async def test_missing_bill_and_partial_failure_availability_are_isolated() -> None:
    customers = (customer("cust-a"), customer("cust-b"))
    entities = await setup_entities(
        customers=customers,
        bills_by_customer_key={"cust-a": bill()},
        errors_by_customer_key={"cust-b": "protocol_error"},
    )
    successful = [entity for entity in entities if entity.unique_id.startswith("cust-a_")]
    failed = [entity for entity in entities if entity.unique_id.startswith("cust-b_")]

    assert len(successful) == 34
    assert len(failed) == 34
    assert {entity.available for entity in successful} == {True}
    assert {entity.available for entity in failed} == {False}
    assert {entity.native_value for entity in failed} == {None}
    assert {tuple(entity.extra_state_attributes) for entity in failed} == {()}
    history_entity = next(
        entity
        for entity in failed
        if entity.entity_description.key == "usage_history_current_month"
    )
    assert history_entity.translation_placeholders == {"year": "----", "month": "--"}


@pytest.mark.asyncio
async def test_super_coordinator_failure_marks_all_entities_unavailable() -> None:
    entities = await setup_entities()
    for entity in entities:
        entity.coordinator.last_update_success = False
    assert {entity.available for entity in entities} == {False}


@pytest.mark.asyncio
async def test_multiple_customer_device_names_are_distinct_and_privacy_safe() -> None:
    customers = (
        customer("cust-a"),
        KepcoCustomer(
            stable_key="cust-b",
            apartment_name="비밀아파트",
            dong="202",
            ho="0304",
            contract_method="아파트(단일계약)",
            is_supported=True,
            _customer_number=f"{RAW_CUSTOMER_SECRET}_b",
            _house_contract_number=f"{RAW_HOUSE_SECRET}_b",
        ),
    )
    entities = await setup_entities(
        customers=customers,
        bills_by_customer_key={"cust-a": bill(), "cust-b": bill()},
    )
    meter_devices = [
        entity.device_info
        for entity in entities
        if entity.entity_description.key == "monthly_usage"
    ]

    assert {device["name"] for device in meter_devices} == {"검침/전기사용량"}
    assert {next(iter(device["identifiers"])) for device in meter_devices} == {
        (DOMAIN, "cust-a"),
        (DOMAIN, "cust-b"),
    }
    rendered = repr([entity.device_info for entity in entities])
    assert RAW_CUSTOMER_SECRET not in rendered
    assert RAW_HOUSE_SECRET not in rendered
    assert RAW_NAME_SECRET not in rendered


@pytest.mark.asyncio
async def test_cleanup_removes_stale_and_removed_entities_and_reenables_legacy_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.kepco_on import sensor as sensor_module

    sensor_any = cast("Any", sensor_module)
    entity_registry = FakeEntityRegistry(
        [
            entity_entry("sensor.stale_monthly", "stale_monthly_usage"),
            entity_entry("sensor.removed_billing_month", "cust-a_billing_month"),
            entity_entry(
                "sensor.reenable_base_charge",
                "cust-a_base_charge",
                disabled_by=RegistryEntryDisabler.INTEGRATION,
            ),
            entity_entry(
                "sensor.keep_user_disabled",
                "cust-a_vat",
                disabled_by=RegistryEntryDisabler.USER,
            ),
            entity_entry("sensor.keep_active", "cust-a_monthly_usage"),
            entity_entry("sensor.unknown", "cust-a_unknown_sensor"),
            entity_entry(
                "sensor.other_platform",
                "stale_monthly_usage",
                platform="other",
            ),
            entity_entry(
                "sensor.other_entry",
                "stale_monthly_usage",
                config_entry_id="entry-2",
            ),
        ]
    )
    device_registry = FakeDeviceRegistry(
        [
            device_entry("device-selected-primary", {(DOMAIN, "cust-a")}),
            device_entry(
                "device-selected-group",
                {(DOMAIN, "cust-a:greenhouse_gas")},
            ),
            device_entry("device-stale-primary", {(DOMAIN, "stale")}),
            device_entry(
                "device-stale-group",
                {(DOMAIN, "stale:monthly_usage")},
            ),
            device_entry(
                "device-stale-shared",
                {(DOMAIN, "stale:electricity_charge")},
                config_entries={"entry-1", "entry-2"},
            ),
            device_entry("device-other", {("other", "stale")}),
            device_entry("device-malformed", {(DOMAIN,)}),
        ]
    )
    monkeypatch.setattr(sensor_any.er, "async_get", lambda hass: entity_registry)
    monkeypatch.setattr(
        sensor_any.er, "async_entries_for_config_entry", lambda reg, entry_id: reg.entries
    )
    monkeypatch.setattr(sensor_any.dr, "async_get", lambda hass: device_registry)
    monkeypatch.setattr(
        sensor_any.dr, "async_entries_for_config_entry", lambda reg, entry_id: reg.entries
    )

    entities = await setup_entities(use_real_registry_cleanup=True)

    assert len(entities) == 34
    assert entity_registry.removed == [
        "sensor.stale_monthly",
        "sensor.removed_billing_month",
    ]
    assert entity_registry.updated == [("sensor.reenable_base_charge", None)]
    assert device_registry.removed == ["device-stale-primary", "device-stale-group"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identifiers", "expected"),
    [
        ({(DOMAIN, "cust-a")}, False),
        ({(DOMAIN, "cust-a:monthly_usage")}, False),
        ({(DOMAIN, "cust-a:greenhouse_gas")}, False),
        ({(DOMAIN, "stale")}, True),
        ({(DOMAIN, "stale:neighbor_comparison")}, True),
        ({("other_domain", "stale")}, False),
        (set(), False),
        ({("other_domain", "stale"), (DOMAIN, "cust-a")}, False),
        ({("other_domain", "stale"), (DOMAIN, "stale")}, True),
        ({(DOMAIN,)}, False),
        ({(DOMAIN, "stale", "extra")}, False),
    ],
)
async def test_async_remove_config_entry_device_only_removes_stale_logical_devices(
    identifiers: set[tuple[str, ...]], expected: bool
) -> None:
    from custom_components.kepco_on.sensor import async_remove_config_entry_device

    coordinator = FakeCoordinator(
        KepcoCoordinatorData(customers=(customer("cust-a"),), bills_by_customer_key={})
    )
    config_entry = entry(coordinator=coordinator)

    result = await async_remove_config_entry_device(
        cast("Any", object()),
        cast("Any", config_entry),
        cast("Any", SimpleNamespace(identifiers=identifiers, config_entries={"entry-1"})),
    )

    assert result is expected


def test_entity_translations_have_json_parity_and_requested_korean_names() -> None:
    strings = json.loads(
        (ROOT / "custom_components/kepco_on/strings.json").read_text(encoding="utf-8")
    )
    english = json.loads(
        (ROOT / "custom_components/kepco_on/translations/en.json").read_text(encoding="utf-8")
    )
    korean = json.loads(
        (ROOT / "custom_components/kepco_on/translations/ko.json").read_text(encoding="utf-8")
    )

    expected_keys = {
        "monthly_usage_period",
        "usage_period_start",
        "usage_period_end",
        "meter_reading_day",
        "meter_reading",
        "previous_meter_reading",
        "monthly_usage",
        "household_usage",
        "common_usage",
        "previous_month_usage",
        "last_year_same_month_usage",
        "current_period_usage",
        "predicted_period_usage",
        "electricity_subtotal",
        "base_charge",
        "energy_charge",
        "climate_environment_charge",
        "fuel_adjustment_charge",
        "child_discount",
        "vat",
        "power_industry_fund",
        "rounding_amount",
        "amount_due",
        "customer_usage",
        "same_building_usage",
        "apartment_total_usage",
        "current_month_co2",
        "previous_month_co2",
        "last_year_same_month_co2",
    }

    assert english["entity"] == strings["entity"]
    assert set(strings["entity"]["sensor"]) == expected_keys
    assert set(korean["entity"]["sensor"]) == expected_keys
    assert korean["entity"]["sensor"] == {
        "monthly_usage_period": {"name": "{year}년 {month}월"},
        "usage_period_start": {"name": "전기 사용 기간 시작일"},
        "usage_period_end": {"name": "전기 사용 기간 종료일"},
        "meter_reading_day": {"name": "검침일"},
        "meter_reading": {"name": "당월지침"},
        "previous_meter_reading": {"name": "전월지침"},
        "monthly_usage": {"name": "당월 사용량"},
        "household_usage": {"name": "당월 세대 사용량"},
        "common_usage": {"name": "당월 공용 사용량"},
        "previous_month_usage": {"name": "전월 사용량"},
        "last_year_same_month_usage": {"name": "전년동월 사용량"},
        "current_period_usage": {"name": "현재 검침기간 누적 사용량"},
        "predicted_period_usage": {"name": "한전 예측 사용량"},
        "electricity_subtotal": {"name": "전기요금\u00a0계"},
        "base_charge": {"name": "전기요금\u00a0상세 기본요금"},
        "energy_charge": {"name": "전기요금\u00a0상세 전력량요금"},
        "climate_environment_charge": {"name": "전기요금\u00a0상세 기후환경요금"},
        "fuel_adjustment_charge": {"name": "전기요금\u00a0상세 연료비조정요금"},
        "child_discount": {"name": "전기요금\u00a0상세 출산가구할인요금"},
        "vat": {"name": "부가가치세"},
        "power_industry_fund": {"name": "전력기금"},
        "rounding_amount": {"name": "원단위절사금액"},
        "amount_due": {"name": "청구금액"},
        "customer_usage": {"name": "고객님"},
        "same_building_usage": {"name": "해당동"},
        "apartment_total_usage": {"name": "아파트 전체"},
        "current_month_co2": {"name": "당월 배출량"},
        "previous_month_co2": {"name": "전월 배출량"},
        "last_year_same_month_co2": {"name": "전년동월 배출량"},
    }
    for language in (strings, english, korean):
        for key in expected_keys:
            sensor_translation = language["entity"]["sensor"][key]
            assert set(sensor_translation) == {"name"}
            assert sensor_translation["name"]


@pytest.mark.asyncio
async def test_history_sensors_expose_billed_amount_only_when_history_carries_it() -> None:
    priced_history = (
        KepcoUsageHistoryPoint("202606", 417, 88450),
        KepcoUsageHistoryPoint("202607", 457, 101230),
        KepcoUsageHistoryPoint("202608", 603),
    )
    sensors = by_key(
        await setup_entities(
            bills_by_customer_key={
                "cust-a": bill(
                    usage_kwh=603,
                    previous_usage_kwh=None,
                    last_year_usage_kwh=None,
                    history=priced_history,
                )
            }
        )
    )

    assert sensors["usage_history_two_months_ago"].extra_state_attributes == {
        "billing_month": "202606",
        "amount_krw": 88450,
    }
    assert sensors["usage_history_previous_month"].extra_state_attributes == {
        "billing_month": "202607",
        "amount_krw": 101230,
    }
    # A point without a billed amount and a month absent from the history stay unchanged.
    assert sensors["usage_history_current_month"].extra_state_attributes == {
        "billing_month": "202608"
    }
    assert sensors["usage_history_last_year_same_month"].extra_state_attributes == {
        "billing_month": "202508"
    }
    # Non-history sensors keep the period attributes and never gain the amount.
    assert sensors["monthly_usage"].extra_state_attributes == {
        "billing_month": "202608",
        "usage_period_start": date(2026, 7, 1),
        "usage_period_end": date(2026, 7, 31),
    }
