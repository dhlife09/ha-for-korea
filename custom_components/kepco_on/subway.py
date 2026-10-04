"""Home Assistant lifecycle and polling for Seoul subway arrivals."""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import cast
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .subway_api import (
    CONF_API_KEY,
    CONF_LINE,
    CONF_STATION,
    DEFAULT_INTERVAL,
    OPT_INTERVAL,
    Arrival,
    RequestBudget,
    SubwayAuthError,
    SubwayError,
    SubwayQuotaError,
    fetch_arrivals,
    validate_settings,
)

_LOGGER = logging.getLogger(__name__)


def get_budget(hass: HomeAssistant, key: str) -> RequestBudget:
    """Share one persistent daily counter per key without storing the key itself."""
    digest = hashlib.sha256(key.encode()).hexdigest()
    budgets: dict[str, RequestBudget] = hass.data.setdefault(f"{DOMAIN}_subway_budgets", {})
    if digest not in budgets:
        budgets[digest] = RequestBudget(Store(hass, 1, f"{DOMAIN}_subway_budget_{digest}"))
    return budgets[digest]


async def async_query(
    hass: HomeAssistant,
    data: dict[str, object],
) -> tuple[Arrival, ...]:
    """Validate transport consent and reserve quota before every network request."""
    validate_settings(data)
    key = str(data[CONF_API_KEY]).strip()
    day = dt_util.utcnow().astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
    await get_budget(hass, key).reserve(day)
    return await fetch_arrivals(
        async_get_clientsession(hass),
        key,
        str(data[CONF_STATION]),
        str(data[CONF_LINE]),
    )


class SubwayCoordinator(DataUpdateCoordinator[tuple[Arrival, ...]]):
    """A station-wide request serves every direction and both upcoming trains."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        interval = int(entry.options.get(OPT_INTERVAL, DEFAULT_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name="Seoul subway arrivals",
            config_entry=entry,
            update_interval=timedelta(seconds=max(120, min(interval, 1800))),
        )
        self.entry = entry

    async def _async_update_data(self) -> tuple[Arrival, ...]:
        try:
            result = await async_query(self.hass, dict(self.entry.data))
            self.update_interval = timedelta(
                seconds=int(self.entry.options.get(OPT_INTERVAL, DEFAULT_INTERVAL))
            )
            return result
        except SubwayAuthError:
            raise ConfigEntryAuthFailed("Seoul API key rejected") from None
        except SubwayQuotaError:
            # Retry infrequently until midnight instead of hammering a depleted key.
            self.update_interval = timedelta(minutes=30)
            raise UpdateFailed("Seoul API daily request budget exhausted") from None
        except SubwayError:
            raise UpdateFailed("Seoul arrival information unavailable") from None


type SubwayConfigEntry = ConfigEntry[SubwayCoordinator]


async def async_setup_subway(hass: HomeAssistant, entry: SubwayConfigEntry) -> bool:
    """Create coordinator and sensor platform using HA's shared HTTP session."""
    coordinator = SubwayCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(async_reload_subway))
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
    return True


async def async_reload_subway(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed credentials or polling options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_subway(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove sensors without closing HA's shared session."""
    return await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR])


def subway_entry(entry: ConfigEntry) -> SubwayConfigEntry:
    """Narrow the entry after checking its service discriminator."""
    return cast("SubwayConfigEntry", entry)
