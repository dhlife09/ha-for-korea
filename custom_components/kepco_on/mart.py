"""Home Assistant lifecycle and sensors for published store holidays."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .mart_api import (
    BRANDS,
    HolidaySchedule,
    MartError,
    Shop,
    fetch_catalog,
    fetch_lotte_holidays,
    month_key,
    next_month,
)

SERVICE = "mart_holidays"
_LOGGER = logging.getLogger(__name__)


def today_in_korea() -> date:
    return dt_util.utcnow().astimezone(ZoneInfo("Asia/Seoul")).date()


def manual_dates(value: str) -> list[str]:
    """Allow an explicit date list for exceptional closures or unpublished stores."""
    tokens = [part for part in re.split(r"[,;\s]+", value.strip()) if part]
    if len(tokens) > 100:
        raise ValueError("Too many holiday dates")
    result = sorted({date.fromisoformat(token).isoformat() for token in tokens})
    if any(not 1900 <= date.fromisoformat(token).year <= 9998 for token in result):
        raise ValueError("Invalid holiday date")
    return result


async def async_catalog(hass: HomeAssistant, brand: str, month: date) -> tuple[Shop, ...]:
    """Share an in-memory public directory for six hours; never cache selected locations."""
    group = "emart" if brand in {"emart", "everyday", "nobrand"} else brand
    key = f"{group}_{month_key(month)}"
    cache: dict[str, tuple[datetime, tuple[Shop, ...]]] = hass.data.setdefault(
        f"{DOMAIN}_mart_catalog", {}
    )
    lock: asyncio.Lock = hass.data.setdefault(f"{DOMAIN}_mart_lock", asyncio.Lock())
    async with lock:
        now = dt_util.utcnow()
        cached = cache.get(key)
        if cached and now - cached[0] < timedelta(hours=6):
            return cached[1]
        shops = await fetch_catalog(async_get_clientsession(hass), brand, month)
        # Drop old months and bound memory even if a calendar is repeatedly reconfigured.
        for old_key, (loaded, _) in tuple(cache.items()):
            if now - loaded >= timedelta(hours=6):
                del cache[old_key]
        cache[key] = (now, shops)
        return shops


async def async_query(
    hass: HomeAssistant, data: Mapping[str, Any], options: Mapping[str, Any]
) -> HolidaySchedule:
    """Read only published dates, or the explicit local manual replacement list."""
    custom = options.get("manual_dates", data.get("manual_dates", []))
    if custom:
        days = tuple(date.fromisoformat(day) for day in manual_dates(",".join(custom)))
        return HolidaySchedule(days, frozenset(month_key(day) for day in days), "manual")
    brand = str(data.get("brand", ""))
    if brand not in BRANDS:
        raise MartError("Unknown store brand")
    today = today_in_korea()
    dates: set[date] = set()
    months: set[str] = set()
    for month in (
        (today, next_month(today)) if brand in {"emart", "everyday", "nobrand"} else (today,)
    ):
        catalog = await async_catalog(hass, brand, month)
        shop = next(
            (item for item in catalog if item.brand == brand and item.id == data.get("store_id")),
            None,
        )
        if shop is None:
            raise MartError("Store no longer listed")
        values = (
            shop.holidays
            if brand in {"emart", "everyday", "nobrand"}
            else await fetch_lotte_holidays(async_get_clientsession(hass), shop)
        )
        dates.update(values)
        months.update(month_key(day) for day in values)
    return HolidaySchedule(tuple(sorted(dates)), frozenset(months))


class MartCoordinator(DataUpdateCoordinator[HolidaySchedule]):
    """A shared response serves all three holiday sensors for one store."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Store holiday dates",
            config_entry=entry,
            update_interval=timedelta(hours=int(entry.options.get("mart_interval_hours", 12))),
        )
        self.entry = entry

    async def _async_update_data(self) -> HolidaySchedule:
        try:
            return await async_query(self.hass, self.entry.data, self.entry.options)
        except MartError, ValueError:
            raise UpdateFailed("Store holiday information unavailable") from None


type MartConfigEntry = ConfigEntry[MartCoordinator]


def mart_entry(entry: ConfigEntry) -> MartConfigEntry:
    return cast("MartConfigEntry", entry)


async def async_setup_mart(hass: HomeAssistant, entry: MartConfigEntry) -> bool:
    coordinator = MartCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
    return True


def async_add_mart_sensors(entry: MartConfigEntry, add: AddEntitiesCallback) -> None:
    add([MartSensor(entry, kind) for kind in ("status", "next", "days")])


class MartSensor(CoordinatorEntity[MartCoordinator], SensorEntity):
    """Report scheduled closures, without claiming a store is currently open."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:store-clock-outline"

    def __init__(self, entry: MartConfigEntry, kind: str) -> None:
        super().__init__(entry.runtime_data)
        self.kind = kind
        self.brand = str(entry.data["brand"])
        self._attr_unique_id = f"{entry.unique_id}_{kind}"
        self._attr_name = {
            "status": "휴무 안내",
            "next": "다음 휴무일",
            "days": "휴무까지 남은 일수",
        }[kind]
        if kind == "next":
            self._attr_device_class = SensorDeviceClass.DATE
        elif kind == "days":
            self._attr_native_unit_of_measurement = "일"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            name=f"{BRANDS[self.brand]} {entry.data['store_name']}",
            manufacturer=BRANDS[self.brand],
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_utc_time_change(self.hass, self.async_midnight, hour=15, minute=0, second=0)
        )

    @callback
    def async_midnight(self, now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | int | date | None:
        data = self.coordinator.data
        today = today_in_korea()
        next_day = next((day for day in data.dates if day >= today), None)
        if self.kind == "next":
            return next_day
        if self.kind == "days":
            return (next_day - today).days if next_day else None
        if today in data.dates:
            return "오늘 휴무"
        if month_key(today) not in data.months:
            return "휴무일 미게시"
        return "등록된 휴무일 아님" if data.source == "manual" else "오늘 정기휴무일 아님"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        lotte_mall = "4" if self.brand == "lottemart" else "5"
        lotte_source = (
            "https://www.lotteon.com/p/lotteplus/offlinestore/offLineStoreInfo"
            f"?mall_no=1&ofln_mall_no={lotte_mall}"
        )
        return {
            "holiday_dates": [day.isoformat() for day in data.dates],
            "schedule_source": data.source,
            "source_url": "https://store.emart.com/branch/list.do"
            if self.brand in {"emart", "everyday", "nobrand"}
            else lotte_source,
        }
