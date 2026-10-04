"""Verify route selections, user isolation, authenticated commands and push lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol
from custom_components.kepco_on.const import DOMAIN
from custom_components.kepco_on.subway_api import CONF_SERVICE, SERVICE, Arrival, SubwayQuotaError
from custom_components.kepco_on.subway_dashboard import (
    DATA_KEY,
    async_setup_dashboard,
    websocket_journey,
    websocket_route,
)
from custom_components.kepco_on.subway_journey import Journey, JourneyManager
from custom_components.kepco_on.subway_network import SubwayNetwork, load_network
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.util import dt as dt_util

MODULE = "custom_components.kepco_on.subway_journey"
NOTIFY = "notify.mobile_app_test_iphone"
START = {
    "command": "start",
    "origin": "a",
    "destination": "d",
    "notify_service": NOTIFY,
    "url": "/lovelace/subway",
}


@pytest.fixture
def small_network() -> SubwayNetwork:
    stations = [
        {"id": "a", "name": "출발", "line": "1006", "group": "a"},
        {"id": "b", "name": "환승", "line": "1006", "group": "b"},
        {"id": "c", "name": "환승", "line": "1007", "group": "b"},
        {"id": "d", "name": "도착", "line": "1007", "group": "d"},
        {"id": "e", "name": "경유", "line": "1006", "group": "e"},
    ]
    edges = [
        {
            "from": "a",
            "to": "b",
            "seconds": 60,
            "transfer": False,
            "direction": "상행",
            "estimated": False,
        },
        {
            "from": "b",
            "to": "c",
            "seconds": 300,
            "transfer": True,
            "direction": "",
            "estimated": True,
        },
        {
            "from": "c",
            "to": "d",
            "seconds": 120,
            "transfer": False,
            "direction": "하행",
            "estimated": False,
        },
        {
            "from": "a",
            "to": "e",
            "seconds": 50,
            "transfer": False,
            "direction": "상행",
            "estimated": False,
        },
        {
            "from": "e",
            "to": "b",
            "seconds": 60,
            "transfer": False,
            "direction": "상행",
            "estimated": False,
        },
    ]
    return SubwayNetwork({"stations": stations, "edges": edges})


@pytest.fixture
def mock_hass() -> MagicMock:
    fake = MagicMock(spec=HomeAssistant)
    fake.data = {}
    fake.services = MagicMock()
    fake.services.async_call = AsyncMock()
    fake.services.has_service.return_value = True
    fake.config_entries = MagicMock()
    fake.config_entries.async_entries.return_value = []
    fake.http = MagicMock()
    fake.http.async_register_static_paths = AsyncMock()
    fake.async_add_executor_job = AsyncMock()
    fake.bus = MagicMock()
    return fake


@pytest.fixture
def manager(mock_hass: MagicMock, small_network: SubwayNetwork) -> JourneyManager:
    return JourneyManager(mock_hass, small_network)


@pytest.fixture(autouse=True)
def mock_timer() -> Iterator[MagicMock]:
    with patch(f"{MODULE}.async_track_time_interval", return_value=MagicMock()) as timer:
        yield timer


def test_real_route_and_vias() -> None:
    network = load_network()
    route = network.route("1006:2647", "1006:2646", [])
    assert route["seconds"] == 70
    assert route["estimated"] is False
    assert route["legs"][0]["direction"] == "상행"
    assert route["legs"][0]["next_station"] == "태릉입구"
    duplicate = network.route("1006:2647", "1006:2646", ["1006:2647", "1006:2647"])
    assert duplicate["platforms"] == route["platforms"]
    assert network.route("1006:2647", "1006:2647", [])["legs"] == []
    with pytest.raises(ValueError, match="알 수 없는"):
        network.group("nonexistent")


def test_route_legs_preserve_transfer_and_via(small_network: SubwayNetwork) -> None:
    route = small_network.route("a", "d", ["e", "e"], "transfers")
    assert route["platforms"] == ["a", "e", "b", "c", "d"]
    assert route["seconds"] == 530
    assert route["transfers"] == 1
    assert route["estimated"] is True
    assert [leg["seconds"] for leg in route["legs"]] == [110, 120]
    assert route["legs"][0]["station_ids"] == ["a", "e", "b"]
    # A transfer origin can choose either actual platform without paying a transfer.
    assert small_network.route("b", "d", [])["transfers"] == 0
    assert small_network.route("a", "a", [])["seconds"] == 0


async def test_journey_start_board_transfer_stop_and_tags(
    manager: JourneyManager,
    mock_hass: MagicMock,
    mock_timer: MagicMock,
) -> None:
    assert await manager.command("user_a", {"command": "status"}) == {"active": False}
    started = await manager.command("user_a", START)
    assert started["phase"] == "waiting"
    assert started["active"] is True
    journey = manager.journeys["user_a"]
    calls = mock_hass.services.async_call.call_args_list
    assert len(calls) == 2
    live = calls[0].args[2]
    control = calls[1].args[2]
    assert live["title"] == "출발 → 도착"
    assert live["data"]["live_update"] is True
    assert live["data"]["url"] == START["url"]
    assert "silent" not in live["data"]
    assert control["data"]["actions"][0]["action"] == journey.tag + "_stop"
    assert control["data"]["tag"] == journey.tag + "_control"
    assert "출발 · 환승 방면" in live["message"]
    mock_timer.assert_called_once()
    await manager.command("user_a", {"command": "board"})
    assert manager.status("user_a")["phase"] == "riding"
    boarded_at = journey.boarded_at
    assert boarded_at is not None
    assert "환승까지 약 1분" in journey.message
    assert mock_hass.services.async_call.call_args.args[2]["data"]["silent"] is True
    await manager.command("user_a", {"command": "next"})
    assert journey.leg_index == 1
    assert manager.status("user_a")["phase"] == "waiting"
    assert journey.boarded_at is None
    assert await manager.command("user_a", {"command": "next"}) == {"active": False}
    cancel = mock_timer.return_value
    cancel.assert_called_once_with()
    assert manager.cancel_timer is None
    cleared = mock_hass.services.async_call.call_args_list[-2:]
    assert [call.args[2]["data"]["tag"] for call in cleared] == [
        journey.tag,
        journey.tag + "_control",
    ]
    assert all(call.args[2]["message"] == "clear_notification" for call in cleared)
    await manager.command("user_a", {"command": "stop"})


async def test_multiuser_isolation_and_notification_action(
    manager: JourneyManager,
    mock_timer: MagicMock,
) -> None:
    await manager.command("a", START)
    await manager.command("b", START)
    first = manager.journeys["a"]
    second = manager.journeys["b"]
    assert first.tag != second.tag
    mock_timer.assert_called_once()
    await manager.notification_action(MagicMock(data={"action": "unknown_stop"}))
    assert len(manager.journeys) == 2
    await manager.notification_action(MagicMock(data={"action": first.tag + "_stop"}))
    assert "a" not in manager.journeys
    assert manager.status("b")["active"] is True
    assert manager.cancel_timer is not None
    await manager.command("b", START)
    assert manager.journeys["b"].tag != second.tag
    await manager.stop("b")


@pytest.mark.parametrize(
    "changes",
    [
        {"notify_service": None},
        {"notify_service": "notify.other"},
        {"notify_service": "notify.mobile_app_a; invalid"},
        {"url": "https://example.com"},
        {"url": "//example.com"},
        {"url": "/a\\b"},
        {"url": "/a\nb"},
        {"url": 7},
        {"origin": "a", "destination": "a"},
    ],
)
async def test_invalid_start_cannot_replace_existing_journey(
    manager: JourneyManager,
    changes: dict[str, Any],
) -> None:
    await manager.command("a", START)
    original = manager.journeys["a"]
    with pytest.raises(ValueError, match=r"알림|주소|다르게"):
        await manager.command("a", {**START, **changes})
    assert manager.journeys["a"] is original


async def test_unknown_service_missing_session_and_unknown_command(
    manager: JourneyManager,
    mock_hass: MagicMock,
) -> None:
    mock_hass.services.has_service.return_value = False
    with pytest.raises(ValueError, match="알림 서비스"):
        await manager.command("a", START)
    for command in ("board", "next", "bad"):
        with pytest.raises(ValueError, match="먼저"):
            await manager.command("a", {"command": command})
    mock_hass.services.has_service.return_value = True
    await manager.command("a", START)
    with pytest.raises(ValueError, match="지원하지"):
        await manager.command("a", {"command": "bad"})


async def test_initial_push_failure_does_not_leave_active_timer(
    manager: JourneyManager,
    mock_hass: MagicMock,
    mock_timer: MagicMock,
) -> None:
    mock_hass.services.async_call.side_effect = HomeAssistantError("synthetic")
    with pytest.raises(ValueError, match="알림을 보내지"):
        await manager.command("a", START)
    assert manager.status("a") == {"active": False}
    mock_timer.assert_not_called()


def arrival(**changes: Any) -> Arrival:
    return replace(
        Arrival(
            "상행",
            "응암",
            "3분 후 도착",
            "이전 역",
            "synthetic",
            "일반",
            180,
            dt_util.utcnow(),
            "99",
            False,
            "1",
        ),
        **changes,
    )


def loaded_entry(**changes: Any) -> MagicMock:
    return MagicMock(
        data={CONF_SERVICE: SERVICE, "api_key": "SYNTHETICKEY", **changes},
        state=ConfigEntryState.LOADED,
    )


async def test_arrivals_use_loaded_entry_and_selected_station(
    manager: JourneyManager,
    mock_hass: MagicMock,
) -> None:
    route = manager.network.route("a", "d", [])
    journey = Journey(route, "mobile_app_test_iphone", "/lovelace/subway")
    assert "먼저 설정" in await manager.arrivals(journey)
    mock_hass.config_entries.async_entries.return_value = [
        loaded_entry(service="other"),
        MagicMock(data={CONF_SERVICE: SERVICE}, state=ConfigEntryState.NOT_LOADED),
        loaded_entry(),
    ]
    with patch(f"{MODULE}.async_query", new_callable=AsyncMock) as query:
        query.return_value = (arrival(),)
        result = await manager.arrivals(journey)
        assert "응암행" in result
        assert "약 3분" in result
        data = query.call_args.args[1]
        assert data["station"] == "출발"
        assert data["line_id"] == "1006"
        assert data["api_key"] == "SYNTHETICKEY"
        mock_hass.config_entries.async_entries.assert_called_with(DOMAIN)
        query.side_effect = SubwayQuotaError("synthetic")
        assert "가져오지 못" in await manager.arrivals(journey)
    journey.route["legs"][0]["line"] = "incheon1"
    assert "지원하지" in await manager.arrivals(journey)
    journey.route["legs"][0]["line"] = "1006"
    journey.route["legs"][0]["direction"] = ""
    assert "지원하지" in await manager.arrivals(journey)


async def test_arrivals_filter_stale_departed_unknown_and_opposite(
    manager: JourneyManager,
    mock_hass: MagicMock,
) -> None:
    mock_hass.config_entries.async_entries.return_value = [loaded_entry()]
    journey = Journey(
        manager.network.route("a", "d", []), "mobile_app_test_iphone", "/lovelace/subway"
    )
    now = dt_util.utcnow()
    bad = (
        arrival(direction="하행"),
        arrival(next_station="다른 지선"),
        arrival(code="2"),
        arrival(generated_at=None),
        arrival(generated_at=now - timedelta(seconds=301)),
        arrival(generated_at=now + timedelta(seconds=61)),
    )
    with patch(f"{MODULE}.async_query", new_callable=AsyncMock) as query:
        query.return_value = bad
        assert "없습니다" in await manager.arrivals(journey)
        local = now.astimezone(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
        query.return_value = (*bad, arrival(generated_at=local, seconds=None))
        result = await manager.arrivals(journey)
        assert "응암행" in result
        assert "약" not in result


async def test_refresh_deduplication_estimate_floor_and_timer_failures(
    manager: JourneyManager,
    mock_hass: MagicMock,
) -> None:
    await manager.command("a", START)
    journey = manager.journeys["a"]
    count = mock_hass.services.async_call.call_count
    await manager.refresh(journey)
    assert mock_hass.services.async_call.call_count == count
    await manager.command("a", {"command": "board"})
    journey.boarded_at = dt_util.utcnow() - timedelta(hours=1)
    await manager.refresh(journey)
    assert "약 0분" in journey.message
    journey.last_message = "force update"
    mock_hass.services.async_call.side_effect = HomeAssistantError("synthetic")
    await manager.tick(dt_util.utcnow())
    assert manager.status("a")["active"] is True
    mock_hass.services.async_call.side_effect = None
    await manager.tick(journey.expires_at)
    assert manager.status("a") == {"active": False}
    await manager.tick(dt_util.utcnow())


async def test_setup_registers_assets_commands_and_listener_once(
    mock_hass: MagicMock,
    small_network: SubwayNetwork,
    mock_timer: MagicMock,
) -> None:
    mock_hass.async_add_executor_job.return_value = small_network
    with patch(
        "custom_components.kepco_on.subway_dashboard.websocket_api.async_register_command"
    ) as register:
        await async_setup_dashboard(mock_hass)
        await async_setup_dashboard(mock_hass)
    assert register.call_count == 2
    mock_hass.http.async_register_static_paths.assert_awaited_once()
    static_path = mock_hass.http.async_register_static_paths.call_args.args[0][0]
    assert static_path.url_path == "/kepco_on"
    assert static_path.path.endswith("frontend")
    mock_hass.bus.async_listen.assert_called_once()
    mock_timer.assert_not_called()


async def test_websocket_commands_auth_errors_and_results(hass: HomeAssistant) -> None:
    network = load_network()
    manager = JourneyManager(hass, network)
    hass.data[DATA_KEY] = manager
    connection = MagicMock(user=MagicMock(id="admin", is_admin=True))
    route_message = {
        "id": 1,
        "type": "kepco_on/subway_route",
        "origin": "1006:2647",
        "destination": "1006:2646",
    }
    schema = cast(Any, websocket_route)._ws_schema
    websocket_route(hass, connection, schema(route_message))
    await hass.async_block_till_done()
    assert connection.send_result.call_args.args[1]["seconds"] == 70
    websocket_route(hass, connection, schema({**route_message, "origin": "unknown"}))
    await hass.async_block_till_done()
    assert connection.send_error.call_args.args[1] == "invalid_route"
    for changes in ({"via": ["x"] * 6}, {"preference": "invalid"}, {"origin": 123}):
        with pytest.raises(vol.Invalid):
            schema({**route_message, **changes})
    journey_message = {"id": 2, "type": "kepco_on/subway_journey", "command": "status"}
    journey_schema = cast(Any, websocket_journey)._ws_schema
    websocket_journey(hass, connection, journey_schema(journey_message))
    await hass.async_block_till_done()
    connection.send_result.assert_called_with(2, {"active": False})
    websocket_journey(hass, connection, journey_schema({**journey_message, "command": "start"}))
    await hass.async_block_till_done()
    assert connection.send_error.call_args.args[1] == "journey_failed"
    for user in (None, MagicMock(is_admin=False)):
        connection.user = user
        with pytest.raises(Unauthorized):
            websocket_journey(hass, connection, journey_schema(journey_message))


async def test_websocket_notify_failure_is_safe(hass: HomeAssistant) -> None:
    manager = MagicMock()
    manager.command = AsyncMock(side_effect=HomeAssistantError("private request URL"))
    hass.data[DATA_KEY] = manager
    connection = MagicMock(user=MagicMock(id="owner", is_admin=True))
    message = {"id": 5, "type": "kepco_on/subway_journey", "command": "status"}
    websocket_journey(hass, connection, message)
    await hass.async_block_till_done()
    manager.command.assert_awaited_once_with("owner", message)
    assert "private" not in str(connection.send_error.call_args)
    assert cast(str, connection.send_error.call_args.args[1]) == "journey_failed"
