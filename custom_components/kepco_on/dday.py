"""Local D-day events, calculated using Home Assistant's calendar date."""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import DOMAIN

SERVICE = "dday"


async def async_setup_dday(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Keep only local event settings as runtime data and register its sensors."""
    entry.runtime_data = validate_settings(entry.data)
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
    return True


def validate_settings(data: Mapping[str, Any]) -> dict[str, Any]:
    """Accept an explicit calendar date and a short event name."""
    name = str(data.get("event_name", "")).strip()
    if not name or len(name) > 60 or any(ord(char) < 32 for char in name):
        raise ValueError("Invalid event name")
    target = date.fromisoformat(str(data.get("target_date", "")))
    if not 1900 <= target.year <= 9998 or type(data.get("yearly", False)) is not bool:
        raise ValueError("Invalid event date")
    return {
        "service": SERVICE,
        "event_name": name,
        "target_date": target.isoformat(),
        "yearly": data.get("yearly", False),
    }


def occurrence(data: Mapping[str, Any], today: date) -> date:
    """For yearly dates, February 29 falls on February 28 in a non-leap year."""
    target = date.fromisoformat(data["target_date"])
    if not data.get("yearly") or today < target:
        return target
    day = min(target.day, calendar.monthrange(today.year, target.month)[1])
    current = date(today.year, target.month, day)
    if current < today:
        year = today.year + 1
        current = date(
            year, target.month, min(target.day, calendar.monthrange(year, target.month)[1])
        )
    return current


def schema(data: Mapping[str, Any] | None = None) -> vol.Schema:
    """Show date selection instead of requiring a YAML template."""
    data = data or {}
    return vol.Schema(
        {
            vol.Required("event_name", default=data.get("event_name", "")): str,
            vol.Required(
                "target_date", default=data.get("target_date", dt_util.now().date().isoformat())
            ): selector.DateSelector(),
            vol.Required("yearly", default=data.get("yearly", False)): bool,
        }
    )


async def async_dday_step(
    flow: config_entries.ConfigFlow,
    user_input: dict[str, Any] | None,
    entry: config_entries.ConfigEntry | None = None,
) -> config_entries.ConfigFlowResult:
    """Store one local event; no HTTP request or server account is involved."""
    errors: dict[str, str] = {}
    if user_input is not None:
        try:
            data = validate_settings(user_input)
        except ValueError:
            errors["base"] = "dday_invalid_date"
        else:
            if entry:
                flow.hass.config_entries.async_update_entry(
                    entry, data=data, title=data["event_name"]
                )
                await flow.hass.config_entries.async_reload(entry.entry_id)
                return flow.async_abort(reason="reconfigure_successful")
            return flow.async_create_entry(title=data["event_name"], data=data)
    return flow.async_show_form(
        step_id="dday", data_schema=schema(entry.data if entry else None), errors=errors
    )


class DdayOptionsFlow(config_entries.OptionsFlow):
    """Edit the same name, date and repetition settings from entry options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = validate_settings(user_input)
            except ValueError:
                errors["base"] = "dday_invalid_date"
            else:
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=data, title=data["event_name"]
                )
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)
                return self.async_create_entry(title=None, data={})
        return self.async_show_form(
            step_id="dday_options", data_schema=schema(self.config_entry.data), errors=errors
        )

    async def async_step_dday_options(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return await self.async_step_init(user_input)


def async_add_dday_sensors(entry: config_entries.ConfigEntry, add: AddEntitiesCallback) -> None:
    """Use the entry ID for stable identity even when its name or date changes."""
    add([DdaySensor(entry, kind) for kind in ("display", "days", "date")])


class DdaySensor(SensorEntity):
    """A local date countdown, updated at local midnight with no polling."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:calendar-star"

    def __init__(self, entry: config_entries.ConfigEntry, kind: str) -> None:
        self.data = validate_settings(entry.data)
        self.kind = kind
        self._attr_unique_id = f"dday_{entry.entry_id}_{kind}"
        self._attr_name = {"display": "디데이", "days": "남은 일수", "date": "목표 날짜"}[kind]
        if kind == "days":
            self._attr_native_unit_of_measurement = "일"
        elif kind == "date":
            self._attr_device_class = SensorDeviceClass.DATE
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"dday_{entry.entry_id}")},
            name=self.data["event_name"],
            manufacturer="HA for Korea",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_track_time_change(self.hass, self.async_midnight, hour=0, minute=0, second=0)
        )

    @callback
    def async_midnight(self, now: datetime) -> None:
        """Recompute both annual occurrences and elapsed days at midnight."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | int | date:
        today = dt_util.now().date()
        target = occurrence(self.data, today)
        days = (target - today).days
        if self.kind == "date":
            return target
        if self.kind == "days":
            return days
        return "D-Day" if days == 0 else f"D-{days}" if days > 0 else f"D+{-days}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "event_name": self.data["event_name"],
            "yearly": self.data["yearly"],
            "target_date": occurrence(self.data, dt_util.now().date()).isoformat(),
        }
