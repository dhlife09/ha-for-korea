"""Daily refresh and setup for monthly RFID food-waste totals."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import cast
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .waste_api import MonthSummary, WasteError, fetch_month, previous_month

_LOGGER = logging.getLogger(__name__)


async def async_query(hass: HomeAssistant, data: Mapping[str, object]) -> tuple[MonthSummary, ...]:
    """Read this month and the previous month in Seoul time."""
    today = dt_util.utcnow().astimezone(ZoneInfo("Asia/Seoul")).date()
    session = async_get_clientsession(hass)
    return tuple(
        [
            await fetch_month(session, data, month, today)
            for month in (today.replace(day=1), previous_month(today))
        ]
    )


class WasteCoordinator(DataUpdateCoordinator[tuple[MonthSummary, ...]]):
    """Monthly summaries, fetched once a day; no raw record persistence."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="RFID monthly food waste",
            config_entry=entry,
            update_interval=timedelta(hours=int(entry.options.get("waste_interval_hours", 24))),
        )
        self.entry = entry

    async def _async_update_data(self) -> tuple[MonthSummary, ...]:
        try:
            return await async_query(self.hass, dict(self.entry.data))
        except WasteError:
            raise UpdateFailed(
                "RFID monthly lookup unavailable; check lookup information"
            ) from None


type WasteConfigEntry = ConfigEntry[WasteCoordinator]


def waste_entry(entry: ConfigEntry) -> WasteConfigEntry:
    """Narrow runtime type after dispatch by service."""
    return cast("WasteConfigEntry", entry)


async def async_setup_waste(hass: HomeAssistant, entry: WasteConfigEntry) -> bool:
    """Start daily polling and create four aggregate sensors."""
    coordinator = WasteCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
    return True


async def async_unload_waste(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unregister sensors, preserving HA's shared HTTP session."""
    return await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR])
