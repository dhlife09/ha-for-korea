"""RFID lookup setup, without account passwords or secret prefilling."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .waste import async_query
from .waste_api import (
    CONF_BUILDING,
    CONF_TAG,
    CONF_UNIT,
    SERVICE,
    WasteError,
    WasteLookupError,
    settings_id,
    validate_settings,
)


async def async_waste_step(
    flow: config_entries.ConfigFlow,
    user_input: dict[str, Any] | None,
    entry: config_entries.ConfigEntry | None = None,
) -> config_entries.ConfigFlowResult:
    """Validate a monthly response; empty lookups never prove identity."""
    errors: dict[str, str] = {}
    if user_input is not None:
        try:
            data = {"service": SERVICE, **validate_settings(user_input)}
            unique_id = settings_id(data)
            # Identity changes create another entry, preserving existing historical sensors.
            if entry is not None and unique_id != entry.unique_id:
                return flow.async_abort(reason="waste_wrong_household")
            await flow.async_set_unique_id(unique_id)
            if entry is None:
                flow._abort_if_unique_id_configured()
            months = await async_query(flow.hass, data)
            if not any(month.count for month in months):
                raise WasteLookupError("No records to verify RFID lookup information")
        except WasteLookupError:
            errors["base"] = "waste_invalid_lookup"
        except WasteError:
            errors["base"] = "waste_cannot_connect"
        else:
            if entry is not None:
                flow.hass.config_entries.async_update_entry(entry, data=data)
                await flow.hass.config_entries.async_reload(entry.entry_id)
                return flow.async_abort(reason="reconfigure_successful")
            return flow.async_create_entry(title="RFID 음식물쓰레기", data=data)
    return flow.async_show_form(
        step_id="waste",
        data_schema=vol.Schema(
            {
                vol.Required(key): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                )
                for key in (CONF_TAG, CONF_BUILDING, CONF_UNIT)
            }
        ),
        errors=errors,
    )


class WasteOptionsFlow(config_entries.OptionsFlowWithReload):
    """Limit automatic refresh to once or twice a day."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=None, data={"waste_interval_hours": int(user_input["waste_interval_hours"])}
            )
        return self.async_show_form(
            step_id="waste_options",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "waste_interval_hours",
                        default=str(self.config_entry.options.get("waste_interval_hours", 24)),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=["12", "24", "48"])
                    )
                }
            ),
        )

    async def async_step_waste_options(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return await self.async_step_init(user_input)
