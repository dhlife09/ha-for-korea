"""Direction-specific next and following train messages."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .subway import SubwayConfigEntry, SubwayCoordinator
from .subway_api import CONF_LINE, CONF_STATION, DIRECTIONS, LINES, Arrival, settings_id


def async_add_subway_sensors(entry: SubwayConfigEntry, add: AddEntitiesCallback) -> None:
    """Create a fixed entity set, also when no trains are currently running."""
    line = str(entry.data[CONF_LINE])
    # Line 2 includes both circular routes and branches with up/down directions.
    directions = DIRECTIONS if line == "1002" else DIRECTIONS[:2]
    sensors = [
        SubwaySensor(entry, direction, index) for direction in directions for index in (0, 1)
    ]
    hass = entry.runtime_data.hass
    registry = er.async_get(hass)
    current_ids = {sensor.unique_id for sensor in sensors}
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id not in current_ids:
            registry.async_remove(entity.entity_id)
    devices = dr.async_get(hass)
    identity = (DOMAIN, settings_id(str(entry.data[CONF_STATION]), line))
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if identity not in device.identifiers:
            devices.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
    add(sensors)


class SubwaySensor(CoordinatorEntity[SubwayCoordinator], SensorEntity):
    """Show API messages without interpreting an unknown ETA as immediate arrival."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:subway-variant"
    _attr_attribution = "서울 열린데이터광장"

    def __init__(self, entry: SubwayConfigEntry, direction: str, index: int) -> None:
        super().__init__(entry.runtime_data)
        station = str(entry.data[CONF_STATION])
        line = str(entry.data[CONF_LINE])
        self.direction = direction
        self.index = index
        self.station = station
        self._attr_unique_id = f"{settings_id(station, line)}_{direction}_{index}"
        self._attr_name = f"{direction} {'다음' if index == 0 else '다다음'} 열차"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, settings_id(station, line))},
            name=f"{station} · {LINES[line]}",
            manufacturer="서울 열린데이터광장",
            model="실시간 지하철 도착정보",
        )

    def _arrival(self) -> Arrival | None:
        now = dt_util.utcnow()
        arrivals = []
        for arrival in self.coordinator.data or ():
            generated = arrival.generated_at
            if generated is None:
                continue
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=ZoneInfo("Asia/Seoul"))
            age = (now - generated).total_seconds()
            if arrival.direction == self.direction and -60 <= age <= 300 and arrival.code != "2":
                arrivals.append(arrival)
        return arrivals[self.index] if len(arrivals) > self.index else None

    @property
    def native_value(self) -> str:
        """The API's train message, or an explicit no-current-information state."""
        arrival = self._arrival()
        return arrival.message if arrival and arrival.message else "도착정보 없음"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose public fields only; never put credentials in an entity."""
        arrival = self._arrival()
        if arrival is None:
            return {"direction": self.direction}
        generated = arrival.generated_at
        if generated is not None and generated.tzinfo is None:
            generated = generated.replace(tzinfo=ZoneInfo("Asia/Seoul"))
        expected = (
            generated + timedelta(seconds=arrival.seconds)
            if generated is not None and arrival.seconds is not None
            else None
        )
        return {
            "direction": arrival.direction,
            "destination": arrival.destination,
            "position": arrival.position,
            "train_number": arrival.train_number,
            "train_type": arrival.train_type,
            "last_train": arrival.last_train,
            "arrival_code": arrival.code,
            "generated_at": generated.isoformat() if generated else None,
            "expected_arrival": expected.isoformat() if expected else None,
            "remaining_seconds": max(0, int((expected - dt_util.utcnow()).total_seconds()))
            if expected
            else None,
        }
