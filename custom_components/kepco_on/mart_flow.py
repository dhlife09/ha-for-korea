"""Brand and exact public-store selection for store holiday lookup."""

from __future__ import annotations

import hashlib
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .mart import SERVICE, async_catalog, async_query, manual_dates, today_in_korea
from .mart_api import BRANDS, MartError, Shop


def settings_id(shop: Shop) -> str:
    return SERVICE + "_" + hashlib.sha256(f"{shop.brand}:{shop.id}".encode()).hexdigest()


def store_schema(
    choices: dict[str, Shop], entry: config_entries.ConfigEntry | None = None
) -> vol.Schema:
    custom = entry.options.get("manual_dates", entry.data.get("manual_dates", [])) if entry else []
    return vol.Schema(
        {
            vol.Required(
                "store_id", default=entry.data.get("store_id", "") if entry else ""
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=shop.id, label=shop.name)
                        for shop in sorted(choices.values(), key=lambda shop: shop.name)
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional("manual_dates", default=", ".join(custom)): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
        }
    )


async def async_search_step(
    flow: config_entries.ConfigFlow,
    user_input: dict[str, Any] | None,
    entry: config_entries.ConfigEntry | None = None,
) -> tuple[config_entries.ConfigFlowResult, dict[str, Shop]]:
    """Filter the anonymous directory locally; search text never leaves HA."""
    errors: dict[str, str] = {}
    if user_input is not None:
        try:
            brand = str(user_input.get("brand", ""))
            if brand not in BRANDS:
                raise MartError("Unknown store brand")
            shops = await async_catalog(flow.hass, brand, today_in_korea())
            search = str(user_input.get("search", "")).strip().casefold()
            choices = {
                shop.id: shop
                for shop in shops
                if shop.brand == brand and search in shop.name.casefold()
            }
            if not choices:
                errors["base"] = "mart_no_stores"
            else:
                return flow.async_show_form(
                    step_id="mart_store", data_schema=store_schema(choices, entry)
                ), choices
        except MartError:
            errors["base"] = "mart_cannot_connect"
    return flow.async_show_form(
        step_id="mart",
        errors=errors,
        data_schema=vol.Schema(
            {
                vol.Required(
                    "brand", default=entry.data.get("brand", "emart") if entry else "emart"
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=brand, label=name)
                            for brand, name in BRANDS.items()
                        ]
                    )
                ),
                vol.Optional("search", default=""): str,
            }
        ),
    ), {}


async def async_store_step(
    flow: config_entries.ConfigFlow,
    user_input: dict[str, Any] | None,
    choices: dict[str, Shop],
    entry: config_entries.ConfigEntry | None = None,
) -> config_entries.ConfigFlowResult:
    if not choices:
        return flow.async_abort(reason="mart_search_again")
    errors: dict[str, str] = {}
    if user_input is not None:
        try:
            shop = choices.get(str(user_input.get("store_id", "")))
            if shop is None:
                raise ValueError("Invalid store")
            unique_id = settings_id(shop)
            if entry and entry.unique_id != unique_id:
                return flow.async_abort(reason="mart_wrong_store")
            custom = manual_dates(str(user_input.get("manual_dates", "")))
            data = {
                "service": SERVICE,
                "brand": shop.brand,
                "store_id": shop.id,
                "store_name": shop.name,
                "manual_dates": custom,
            }
            await flow.async_set_unique_id(unique_id)
            if entry is None:
                flow._abort_if_unique_id_configured()
            await async_query(flow.hass, data, {})
        except ValueError:
            errors["base"] = "mart_invalid_dates"
        except MartError:
            errors["base"] = "mart_cannot_connect"
        else:
            title = f"{BRANDS[shop.brand]} {shop.name}"
            if entry:
                options = {"mart_interval_hours": entry.options.get("mart_interval_hours", 12)}
                flow.hass.config_entries.async_update_entry(
                    entry, data=data, title=title, options=options
                )
                await flow.hass.config_entries.async_reload(entry.entry_id)
                return flow.async_abort(reason="reconfigure_successful")
            return flow.async_create_entry(title=title, data=data)
    return flow.async_show_form(
        step_id="mart_store", data_schema=store_schema(choices, entry), errors=errors
    )


class MartOptionsFlow(config_entries.OptionsFlowWithReload):
    """Update manual replacement dates and the public-information refresh interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                dates = manual_dates(str(user_input.get("manual_dates", "")))
                interval = int(user_input.get("mart_interval_hours", 12))
                if interval not in {6, 12, 24}:
                    raise ValueError
            except ValueError:
                errors["base"] = "mart_invalid_dates"
            else:
                return self.async_create_entry(
                    title=None, data={"manual_dates": dates, "mart_interval_hours": interval}
                )
        custom = self.config_entry.options.get(
            "manual_dates", self.config_entry.data.get("manual_dates", [])
        )
        return self.async_show_form(
            step_id="mart_options",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional("manual_dates", default=", ".join(custom)): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True)
                    ),
                    vol.Required(
                        "mart_interval_hours",
                        default=str(self.config_entry.options.get("mart_interval_hours", 12)),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=["6", "12", "24"])
                    ),
                }
            ),
        )

    async def async_step_mart_options(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return await self.async_step_init(user_input)
