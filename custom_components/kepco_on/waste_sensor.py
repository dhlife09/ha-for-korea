"""Aggregate monthly food-waste sensors with no household identifiers."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfMass
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .waste import WasteConfigEntry, WasteCoordinator
from .waste_api import previous_month


def async_add_waste_sensors(entry: WasteConfigEntry, add: AddEntitiesCallback) -> None:
    """One entity per month offset and statistic, stable across month rollover."""
    add([WasteSensor(entry, index, count) for index in (0, 1) for count in (False, True)])


class WasteSensor(CoordinatorEntity[WasteCoordinator], SensorEntity):
    """This/last month totals; successful empty months have zero disposal."""

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

    async def async_added_to_hass(self) -> None:
        """Invalidate obsolete month labels at midnight in Seoul without another query."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_utc_time_change(
                self.hass, self.async_month_rollover, hour=15, minute=0, second=0
            )
        )

    @callback
    def async_month_rollover(self, now: datetime) -> None:
        """Update cached HA state when the requested month changes."""
        self.async_write_ha_state()

    @property
    def expected_month(self) -> str:
        """Do not label an old monthly total as this month after midnight rollover."""
        today = dt_util.utcnow().astimezone(ZoneInfo("Asia/Seoul")).date()
        return (previous_month(today) if self.index else today).strftime("%Y-%m")

    @property
    def native_value(self) -> Decimal | int | None:
        data = self.coordinator.data
        if not data or len(data) <= self.index:
            return None
        month = data[self.index]
        if month.month != self.expected_month:
            return None
        return month.count if self.count else month.kilograms

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data or len(data) <= self.index:
            return {}
        month = data[self.index]
        status = "ok" if month.count else "no_records"
        if month.month != self.expected_month:
            status = "stale_month"
        return {"month": month.month, "lookup_status": status}
