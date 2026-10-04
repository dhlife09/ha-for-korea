"""Independent subway implementation tests with synthetic station and key data."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession
from custom_components.kepco_on.const import DOMAIN
from custom_components.kepco_on.subway import (
    SubwayCoordinator,
    async_query,
    async_setup_subway,
    async_unload_subway,
    get_budget,
)
from custom_components.kepco_on.subway_api import (
    CONF_API_KEY,
    CONF_HTTP,
    CONF_LINE,
    CONF_SERVICE,
    CONF_STATION,
    DAILY_LIMIT,
    OPT_INTERVAL,
    SERVICE,
    RequestBudget,
    SubwayAuthError,
    SubwayError,
    SubwayQuotaError,
    fetch_arrivals,
    parse_response,
    settings_id,
    validate_settings,
)
from custom_components.kepco_on.subway_sensor import SubwaySensor, async_add_subway_sensors
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytestmark = pytest.mark.usefixtures("socket_enabled")

SETTINGS = {
    CONF_SERVICE: SERVICE,
    CONF_API_KEY: "SYNTHETICKEYFORTEST000",
    CONF_STATION: "테스트역A",
    CONF_LINE: "1006",
    CONF_HTTP: True,
}


def row(**changes: Any) -> dict[str, Any]:
    """A generated public record; never a captured user response."""
    return {
        "subwayId": "1006",
        "statnNm": SETTINGS[CONF_STATION],
        "updnLine": "상행",
        "bstatnNm": "테스트종점",
        "btrainNo": "TEST001",
        "arvlMsg2": "3분 후 도착",
        "arvlMsg3": "테스트전역",
        "barvlDt": "180",
        "recptnDt": "2026-01-01 12:00:00",
        "arvlCd": "99",
        "ordkey": "11001",
        **changes,
    }


def payload(*records: Any) -> dict[str, Any]:
    return {"errorMessage": {"code": "INFO-000"}, "realtimeArrivalList": list(records)}


def parse(*records: dict[str, Any]) -> Any:
    return parse_response(payload(*records), str(SETTINGS[CONF_STATION]), "1006")


def entry(**changes: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=settings_id(str(SETTINGS[CONF_STATION]), "1006"),
        version=3,
        data={**SETTINGS, **changes},
        options={OPT_INTERVAL: 120},
    )


def test_parse_filters_orders_deduplicates_and_preserves_unknown_eta() -> None:
    records = parse(
        row(btrainNo="TEST002", ordkey="12001", barvlDt="0", lstcarAt="1"),
        row(),
        row(),
        row(subwayId="1007"),
        row(statnNm="별도역"),
        row(updnLine="unknown"),
    )
    assert len(records) == 2
    assert records[0].seconds == 180
    assert records[1].seconds is None
    assert records[1].last_train is True
    assert records[0].destination == "테스트종점"


@pytest.mark.parametrize("seconds", ["", "-1", "NaN", None])
def test_invalid_eta_is_unknown(seconds: object) -> None:
    assert parse(row(barvlDt=seconds, recptnDt="bad"))[0].seconds is None
    assert parse(row(recptnDt="bad"))[0].generated_at is None


@pytest.mark.parametrize(
    "data",
    [None, [], {}, {"errorMessage": []}, payload() | {"realtimeArrivalList": None}, payload(None)],
)
def test_malformed_response_is_rejected(data: object) -> None:
    with pytest.raises(SubwayError):
        parse_response(data, "테스트역A", "1006")


@pytest.mark.parametrize(
    ("code", "exception"),
    [("INFO-100", SubwayAuthError), ("ERROR-337", SubwayQuotaError), ("ERROR-500", SubwayError)],
)
def test_api_errors_are_fixed_messages(code: str, exception: type[Exception]) -> None:
    with pytest.raises(exception) as caught:
        parse_response(
            {"RESULT": {"CODE": code, "MESSAGE": "PRIVATE_URL_CANARY"}}, "테스트역A", "1006"
        )
    assert "PRIVATE_URL_CANARY" not in str(caught.value)


def test_valid_empty_response() -> None:
    assert parse_response({"RESULT": {"CODE": "INFO-200"}}, "테스트역A", "1006") == ()
    assert parse() == ()
    assert parse_response({"code": "INFO-200"}, "테스트역A", "1006") == ()


@pytest.mark.parametrize(
    "changes",
    [
        {CONF_API_KEY: "bad/key"},
        {CONF_STATION: "a/b"},
        {CONF_STATION: ""},
        {CONF_LINE: "unknown"},
        {CONF_HTTP: False},
    ],
)
def test_settings_validation_prevents_injection_and_requires_consent(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(SubwayError):
        validate_settings({**SETTINGS, **changes})
    validate_settings(SETTINGS)
    assert "테스트" not in settings_id("테스트역A", "1006")


async def test_budget_survives_restart_and_serializes_calls() -> None:
    store = MagicMock()
    store.async_load = AsyncMock(return_value={"day": "2026-01-01", "count": DAILY_LIMIT - 1})
    store.async_save = AsyncMock()
    budget = RequestBudget(store)
    results = await asyncio.gather(
        budget.reserve("2026-01-01"), budget.reserve("2026-01-01"), return_exceptions=True
    )
    assert sum(isinstance(value, SubwayQuotaError) for value in results) == 1
    assert store.async_save.await_count == 1
    await budget.reserve("2026-01-02")
    assert budget.state == {"day": "2026-01-02", "count": 1}


async def test_network_client_uses_only_seoul_and_no_redirects(aresponses: Any) -> None:
    aresponses.add(
        "swopenapi.seoul.go.kr",
        "/api/subway/SYNTHETICKEYFORTEST000/json/realtimeStationArrival/0/1000/테스트역A",
        "get",
        aresponses.Response(
            text=json.dumps(payload(row())), headers={"Content-Type": "application/json"}
        ),
    )
    async with ClientSession() as session:
        records = await fetch_arrivals(session, str(SETTINGS[CONF_API_KEY]), "테스트역A", "1006")
    assert len(records) == 1
    aresponses.assert_plan_strictly_followed()


@pytest.mark.parametrize(
    ("status", "exception"),
    [
        (302, SubwayError),
        (401, SubwayAuthError),
        (403, SubwayAuthError),
        (429, SubwayQuotaError),
        (500, SubwayError),
    ],
)
async def test_http_errors_do_not_disclose_key(
    aresponses: Any, status: int, exception: type[Exception]
) -> None:
    aresponses.add(
        "swopenapi.seoul.go.kr",
        "/api/subway/SYNTHETICKEYFORTEST000/json/realtimeStationArrival/0/1000/테스트역A",
        "get",
        aresponses.Response(status=status, headers={"Location": "http://third-party.invalid/"}),
    )
    async with ClientSession() as session:
        with pytest.raises(exception) as caught:
            await fetch_arrivals(session, str(SETTINGS[CONF_API_KEY]), "테스트역A", "1006")
    assert str(SETTINGS[CONF_API_KEY]) not in str(caught.value)


async def test_query_shares_budget_and_validates_before_transmission(hass: Any) -> None:
    assert get_budget(hass, "test_key") is get_budget(hass, "test_key")
    with (
        patch(
            "custom_components.kepco_on.subway.fetch_arrivals", new=AsyncMock(return_value=())
        ) as fetch,
        patch(
            "custom_components.kepco_on.subway.async_resolve_station",
            new=AsyncMock(return_value="테스트역A(테스트학교)"),
        ),
    ):
        await async_query(hass, SETTINGS)
        assert fetch.await_count == 1
        assert fetch.await_args is not None
        assert fetch.await_args.args[2] == "테스트역A(테스트학교)"
        with pytest.raises(SubwayError):
            await async_query(hass, {**SETTINGS, CONF_HTTP: False})
        assert fetch.await_count == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SubwayAuthError("safe"), ConfigEntryAuthFailed),
        (SubwayQuotaError("safe"), UpdateFailed),
        (SubwayError("safe"), UpdateFailed),
    ],
)
async def test_coordinator_maps_safe_failures(
    hass: Any, error: Exception, expected: type[Exception]
) -> None:
    coordinator = SubwayCoordinator(hass, entry())
    with (
        patch("custom_components.kepco_on.subway.async_query", new=AsyncMock(side_effect=error)),
        pytest.raises(expected),
    ):
        await coordinator._async_update_data()
    if isinstance(error, SubwayQuotaError):
        assert coordinator.update_interval == timedelta(minutes=30)


async def test_lifecycle_and_sensor_values(hass: Any) -> None:
    config = entry()
    config.add_to_hass(hass)
    config.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    records = parse(row(), row(updnLine="하행", btrainNo="TEST003", barvlDt="0"))
    with (
        patch("custom_components.kepco_on.subway.async_query", new=AsyncMock(return_value=records)),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()),
    ):
        assert await async_setup_subway(hass, config)
    add = MagicMock()
    async_add_subway_sensors(config, add)
    assert len(add.call_args.args[0]) == 4
    sensor = SubwaySensor(config, "상행", 0)
    with patch(
        "custom_components.kepco_on.subway_sensor.dt_util.utcnow",
        return_value=datetime(2026, 1, 1, 3, 0, 30, tzinfo=UTC),
    ):
        assert sensor.native_value == "3분 후 도착"
        assert sensor.extra_state_attributes["remaining_seconds"] == 150
        assert "api_key" not in sensor.extra_state_attributes
        assert SubwaySensor(config, "하행", 0).extra_state_attributes["remaining_seconds"] is None
        assert SubwaySensor(config, "상행", 1).native_value == "도착정보 없음"
    with patch(
        "custom_components.kepco_on.subway_sensor.dt_util.utcnow",
        return_value=datetime(2026, 1, 1, 3, 6, tzinfo=UTC),
    ):
        assert sensor.native_value == "도착정보 없음"
    with patch.object(
        hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=True)
    ):
        assert await async_unload_subway(hass, config)


async def test_real_ha_setup_unload_and_private_diagnostics(
    hass: Any,
    enable_custom_integrations: None,
) -> None:
    """Exercise dispatch through HA itself, including actual entity registration."""
    from custom_components.kepco_on.diagnostics import async_get_config_entry_diagnostics

    config = entry()
    config.add_to_hass(hass)
    with patch(
        "custom_components.kepco_on.subway.async_query", new=AsyncMock(return_value=parse(row()))
    ):
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
    states = hass.states.async_all("sensor")
    assert len(states) == 4
    assert all(
        str(SETTINGS[CONF_API_KEY]) not in json.dumps(state.as_dict(), ensure_ascii=False)
        for state in states
    )
    diagnostics = await async_get_config_entry_diagnostics(hass, config)
    assert diagnostics["arrival_count"] == 1
    serialized = json.dumps(diagnostics, ensure_ascii=False)
    assert str(SETTINGS[CONF_STATION]) not in serialized
    assert str(SETTINGS[CONF_API_KEY]) not in serialized
    assert await hass.config_entries.async_unload(config.entry_id)
    await hass.async_block_till_done()


async def test_user_menu_and_subway_registration() -> None:
    from tests.test_config_flow import make_flow

    flow = make_flow()
    menu = await flow.async_step_user()
    assert menu["menu_options"] == ["kepco", "subway"]
    form = await flow.async_step_subway()
    assert form["step_id"] == "subway"
    with patch(
        "custom_components.kepco_on.subway_flow.async_query", new=AsyncMock(return_value=())
    ):
        result = await flow.async_step_subway(SETTINGS)
    assert result["type"] == "create_entry"
    assert result["data"] == SETTINGS
    assert str(SETTINGS[CONF_API_KEY]) not in result["title"]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (SubwayAuthError("safe"), "subway_invalid_key"),
        (SubwayQuotaError("safe"), "subway_quota"),
        (SubwayError("safe"), "subway_cannot_connect"),
    ],
)
async def test_subway_flow_safe_errors(error: Exception, message: str) -> None:
    from tests.test_config_flow import make_flow

    flow = make_flow()
    with patch(
        "custom_components.kepco_on.subway_flow.async_query", new=AsyncMock(side_effect=error)
    ):
        result = await flow.async_step_subway(SETTINGS)
    assert result["errors"] == {"base": message}
    assert result["step_id"] == "subway"


@pytest.mark.parametrize("source", ["reauth", "reconfigure"])
async def test_subway_reauthentication_and_reconfigure(source: str) -> None:
    from tests.test_config_flow import FakeConfigEntry, make_flow

    flow = make_flow()
    config = FakeConfigEntry(
        data=SETTINGS, unique_id=settings_id(str(SETTINGS[CONF_STATION]), "1006")
    )
    flow.hass.config_entries.entries_by_id[config.entry_id] = config
    flow.hass.config_entries.async_reload = AsyncMock()
    flow.context = {"source": source, "entry_id": config.entry_id}
    if source == "reauth":
        form = await flow.async_step_reauth(SETTINGS)
    else:
        form = await flow.async_step_reconfigure()
    assert form["step_id"] == "subway"
    with patch(
        "custom_components.kepco_on.subway_flow.async_query", new=AsyncMock(return_value=())
    ):
        result = await flow.async_step_subway(
            {**SETTINGS, CONF_API_KEY: "REPLACEMENTKEYFORTEST000"}
        )
    assert result["reason"] == (
        "reauth_successful" if source == "reauth" else "reconfigure_successful"
    )
    assert config.data[CONF_API_KEY] == "REPLACEMENTKEYFORTEST000"
    assert flow.hass.config_entries.async_reload.await_count == 1
    if source == "reauth":
        result = await flow.async_step_subway({**SETTINGS, CONF_STATION: "별도역"})
        assert result["reason"] == "subway_wrong_station"


async def test_subway_options_flow(hass: Any) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow
    from custom_components.kepco_on.subway_flow import SubwayOptionsFlow

    config = entry()
    flow = KepcoOnConfigFlow.async_get_options_flow(config)
    assert isinstance(flow, SubwayOptionsFlow)
    config.add_to_hass(hass)
    flow.hass = hass
    flow.handler = config.entry_id
    result = await flow.async_step_init()
    assert result["step_id"] == "subway_options"
    result = await flow.async_step_subway_options({OPT_INTERVAL: "300"})
    assert result["data"] == {OPT_INTERVAL: 300}


@pytest.mark.parametrize("body", ["invalid-json", " " * 2_000_001])
async def test_untrusted_bodies_are_bounded_and_errors_are_safe(aresponses: Any, body: str) -> None:
    aresponses.add(
        "swopenapi.seoul.go.kr",
        "/api/subway/SYNTHETICKEYFORTEST000/json/realtimeStationArrival/0/1000/테스트역A",
        "get",
        aresponses.Response(text=body),
    )
    async with ClientSession() as session:
        with pytest.raises(SubwayError) as caught:
            await fetch_arrivals(session, str(SETTINGS[CONF_API_KEY]), "테스트역A", "1006")
    assert "SYNTHETICKEYFORTEST000" not in str(caught.value)


async def test_station_reconfigure_updates_identity_and_removes_old_entities(
    hass: Any,
    enable_custom_integrations: None,
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    config = entry()
    config.add_to_hass(hass)
    with patch("custom_components.kepco_on.subway.async_query", new=AsyncMock(return_value=())):
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
    old_ids = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(er.async_get(hass), config.entry_id)
    }
    flow = KepcoOnConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.context = {"source": "reconfigure", "entry_id": config.entry_id}
    with (
        patch("custom_components.kepco_on.subway_flow.async_query", new=AsyncMock(return_value=())),
        patch("custom_components.kepco_on.subway.async_query", new=AsyncMock(return_value=())),
    ):
        result = await flow.async_step_subway({**SETTINGS, CONF_STATION: "별도역"})
        await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert config.unique_id == settings_id("별도역", "1006")
    new_entities = er.async_entries_for_config_entry(er.async_get(hass), config.entry_id)
    assert len(new_entities) == 4
    assert all(entity.unique_id not in old_ids for entity in new_entities)
    assert len(dr.async_entries_for_config_entry(dr.async_get(hass), config.entry_id)) == 1
    assert await hass.config_entries.async_unload(config.entry_id)
    await hass.async_block_till_done()


async def test_subway_reload_listener_and_line_two_branches(hass: Any) -> None:
    from custom_components.kepco_on.subway import async_reload_subway

    config = entry(line_id="1002")
    config.runtime_data = SubwayCoordinator(hass, config)
    add = MagicMock()
    async_add_subway_sensors(config, add)
    assert len(add.call_args.args[0]) == 8
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload:
        await async_reload_subway(hass, config)
        reload.assert_awaited_once_with(config.entry_id)
