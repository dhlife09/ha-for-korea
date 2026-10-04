"""Aggregate monthly food-waste sensors with no household identifiers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfMass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .waste import WasteConfigEntry, WasteCoordinator


def async_add_waste_sensors(entry: WasteConfigEntry, add: AddEntitiesCallback) -> None:
    """One entity per month offset and statistic, stable across month rollover."""
    add([WasteSensor(entry, index, count) for index in (0, 1) for count in (False, True)])


class WasteSensor(CoordinatorEntity[WasteCoordinator], SensorEntity):
    """This/last month totals; an empty lookup remains unknown instead of zero."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: WasteConfigEntry, index: int, count: bool) -> None:
        super().__init__(entry.runtime_data)
        self.index, self.count = index, count
        self._attr_unique_id = f"{entry.unique_id}_{index}_{'count' if count else 'kg'}"
        self._attr_name = (
            f"{'이번 달' if index == 0 else '지난달'} {'배출 횟수' if count else '배출량'}"
        )
        self._attr_icon = "mdi:delete-outline"
        if count:
            self._attr_native_unit_of_measurement = "회"
        else:
            self._attr_device_class = SensorDeviceClass.WEIGHT
            self._attr_native_unit_of_measurement = UnitOfMass.KILOGRAMS
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            name="RFID 음식물쓰레기",
            manufacturer="한국환경공단",
        )

    @property
    def native_value(self) -> Decimal | int | None:
        data = self.coordinator.data
        if not data or len(data) <= self.index:
            return None
        month = data[self.index]
        if month.kilograms is None:
            return None
        return month.count if self.count else month.kilograms

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data or len(data) <= self.index:
            return {}
        month = data[self.index]
        return {"month": month.month, "lookup_status": "ok" if month.count else "no_records"}
