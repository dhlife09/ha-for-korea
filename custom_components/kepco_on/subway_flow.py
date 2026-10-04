"""Setup and options for Seoul subway; no embedded station or credential defaults."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .subway import async_query
from .subway_api import (
    CONF_API_KEY,
    CONF_HTTP,
    CONF_LINE,
    CONF_SERVICE,
    CONF_STATION,
    DEFAULT_INTERVAL,
    LINES,
    OPT_INTERVAL,
    SERVICE,
    SubwayAuthError,
    SubwayError,
    SubwayQuotaError,
    normalize_station,
    settings_id,
)
from .subway_catalog import SubwayStationError


def subway_schema(data: dict[str, Any] | None = None) -> vol.Schema:
    """Only preload non-secret settings; API keys always use a password control."""
    data = data or {}
    return vol.Schema(
        {
            vol.Required(CONF_API_KEY): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD),
            ),
            vol.Required(CONF_STATION, default=data.get(CONF_STATION, "")): str,
            vol.Required(CONF_LINE, default=data.get(CONF_LINE, "1001")): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=value, label=label)
                        for value, label in LINES.items()
                    ]
                ),
            ),
            vol.Required(CONF_HTTP, default=False): vol.All(bool, vol.In([True])),
        }
    )


async def async_subway_step(
    flow: config_entries.ConfigFlow,
    user_input: dict[str, Any] | None,
    entry: config_entries.ConfigEntry | None = None,
    *,
    reauth: bool = False,
) -> config_entries.ConfigFlowResult:
    """Check credentials and public data without persisting live responses."""
    errors: dict[str, str] = {}
    if user_input is not None:
        data = {
            CONF_SERVICE: SERVICE,
            CONF_API_KEY: str(user_input.get(CONF_API_KEY, "")).strip(),
            CONF_STATION: normalize_station(str(user_input.get(CONF_STATION, ""))),
            CONF_LINE: str(user_input.get(CONF_LINE, "")),
            CONF_HTTP: user_input.get(CONF_HTTP, False),
        }
        unique_id = settings_id(data[CONF_STATION], data[CONF_LINE])
        if reauth and entry and unique_id != entry.unique_id:
            return flow.async_abort(reason="subway_wrong_station")
        await flow.async_set_unique_id(unique_id)
        if entry is None or unique_id != entry.unique_id:
            flow._abort_if_unique_id_configured()
        try:
            await async_query(flow.hass, data)
        except SubwayAuthError:
            errors["base"] = "subway_invalid_key"
        except SubwayQuotaError:
            errors["base"] = "subway_quota"
        except SubwayStationError:
            errors["base"] = "subway_invalid_station"
        except SubwayError:
            errors["base"] = "subway_cannot_connect"
        else:
            title = f"{data[CONF_STATION]} · {LINES[data[CONF_LINE]]}"
            if entry is not None:
                flow.hass.config_entries.async_update_entry(
                    entry,
                    data=data,
                    title=title,
                    unique_id=unique_id,
                )
                await flow.hass.config_entries.async_reload(entry.entry_id)
                return flow.async_abort(
                    reason="reauth_successful" if reauth else "reconfigure_successful"
                )
            return flow.async_create_entry(title=title, data=data)
    return flow.async_show_form(
        step_id="subway",
        data_schema=subway_schema(dict(entry.data) if entry else None),
        errors=errors,
    )


class SubwayOptionsFlow(config_entries.OptionsFlowWithReload):
    """Set station polling interval with a safe minimum."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=None,
                data={
                    OPT_INTERVAL: int(user_input[OPT_INTERVAL]),
                },
            )
        return self.async_show_form(
            step_id="subway_options",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPT_INTERVAL,
                        default=str(
                            self.config_entry.options.get(
                                OPT_INTERVAL,
                                DEFAULT_INTERVAL,
                            )
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["120", "180", "300", "600", "1800"],
                        )
                    ),
                }
            ),
        )

    async def async_step_subway_options(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        return await self.async_step_init(user_input)
