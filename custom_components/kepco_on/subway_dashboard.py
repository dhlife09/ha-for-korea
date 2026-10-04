"""Authenticated dashboard WebSocket commands and bundled frontend assets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.http.server import StaticPathConfig
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import (
    async_response,
    require_admin,
    websocket_command,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .subway_journey import JourneyManager
from .subway_network import load_network

DATA_KEY = f"{DOMAIN}_subway_dashboard"
STOP_SCHEMA: dict[Any, Any] = {
    vol.Required("origin"): str,
    vol.Required("destination"): str,
    vol.Optional("via", default=[]): vol.All([str], vol.Length(max=5)),
    vol.Optional("preference", default="time"): vol.In(("time", "transfers")),
}


@websocket_command({vol.Required("type"): "kepco_on/subway_route", **STOP_SCHEMA})
@async_response
async def websocket_route(
    hass: HomeAssistant, connection: ActiveConnection, message: dict[str, Any]
) -> None:
    """Return public route data without exposing config-entry credentials."""
    manager: JourneyManager = hass.data[DATA_KEY]
    try:
        result = manager.network.route(
            message["origin"], message["destination"], message["via"], message["preference"]
        )
    except ValueError:
        connection.send_error(message["id"], "invalid_route", "선택한 역을 연결할 수 없습니다.")
    else:
        connection.send_result(message["id"], result)


@websocket_command(
    {
        vol.Required("type"): "kepco_on/subway_journey",
        vol.Required("command"): vol.In(("start", "board", "next", "stop", "status")),
        vol.Optional("origin"): str,
        vol.Optional("destination"): str,
        vol.Optional("via", default=[]): vol.All([str], vol.Length(max=5)),
        vol.Optional("preference", default="time"): vol.In(("time", "transfers")),
        vol.Optional("notify_service"): str,
        vol.Optional("url"): str,
    }
)
@require_admin
@async_response
async def websocket_journey(
    hass: HomeAssistant, connection: ActiveConnection, message: dict[str, Any]
) -> None:
    """Only admins can select a notification recipient; sessions are user-scoped."""
    manager: JourneyManager = hass.data[DATA_KEY]
    assert connection.user is not None
    try:
        result = await manager.command(connection.user.id, message)
    except ValueError, KeyError, HomeAssistantError:
        connection.send_error(
            message["id"], "journey_failed", "경로 또는 아이폰 알림 설정을 확인해 주세요."
        )
    else:
        connection.send_result(message["id"], result)


async def async_setup_dashboard(hass: HomeAssistant) -> None:
    """Register assets once; no credentials are included in the static folder."""
    if DATA_KEY in hass.data:
        return
    network = await hass.async_add_executor_job(load_network)
    manager = JourneyManager(hass, network)
    hass.data[DATA_KEY] = manager
    websocket_api.async_register_command(hass, websocket_route)
    websocket_api.async_register_command(hass, websocket_journey)
    await hass.http.async_register_static_paths(
        [StaticPathConfig("/kepco_on", str(Path(__file__).parent / "frontend"), False)]
    )
    manager.setup()
