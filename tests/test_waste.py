"""RFID integration tests using generated household identifiers and records."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession
from custom_components.kepco_on.const import DOMAIN
from custom_components.kepco_on.waste import WasteCoordinator, async_query
from custom_components.kepco_on.waste_api import (
    CONF_BUILDING,
    CONF_TAG,
    CONF_UNIT,
    MAX_BODY,
    SERVICE,
    MonthSummary,
    WasteError,
    WasteLookupError,
    fetch_month,
    parse_page,
    previous_month,
    settings_id,
    validate_settings,
)
from custom_components.kepco_on.waste_sensor import WasteSensor
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytestmark = pytest.mark.usefixtures("socket_enabled")
SETTINGS = {"service": SERVICE, CONF_TAG: "SYNTHETICTAG000", CONF_BUILDING: "999", CONF_UNIT: "888"}
MONTH = date(2026, 1, 1)
SUMMARIES = (MonthSummary("2026-01", Decimal("1.25"), 2), MonthSummary("2025-12", Decimal(0), 0))
PATH = "/portal/status/selectDischargerQuantityQuickMonthNew.do"


def payload(page: int = 1, total: int = 2, size: int = 10) -> dict[str, Any]:
    count = min(size, max(0, total - (page - 1) * size))
    return {
        "msgCode": "success",
        "totalCnt": total,
        "ctznnm": "SYNTHETIC PRIVATE NAME",
        "paginationInfo": {
            "totalPageCount": (total + size - 1) // size,
            "currentPageNo": page,
            "recordCountPerPage": size,
            "totalRecordCount": total,
        },
        "list": [
            {"label": "01월", "dttime": f"2026-01-{page:02} 12:00:00", "qtyvalue": "0.15"}
            for _ in range(count)
        ],
    }


def entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, version=3, unique_id=settings_id(SETTINGS), data=SETTINGS)


def test_identity_months_and_decimal_records() -> None:
    assert previous_month(MONTH) == date(2025, 12, 1)
    assert previous_month(date(2026, 3, 1)) == date(2026, 2, 1)
    assert SETTINGS[CONF_TAG] not in settings_id(SETTINGS)
    assert (
        validate_settings({**SETTINGS, CONF_TAG: " SYNTHETICTAG000 "})[CONF_TAG]
        == SETTINGS[CONF_TAG]
    )
    assert parse_page(payload(), MONTH, 1) == (2, 1, [Decimal("0.15"), Decimal("0.15")])


@pytest.mark.parametrize(
    ("key", "value"),
    [(CONF_TAG, "../bad"), (CONF_TAG, ""), (CONF_BUILDING, "abc"), (CONF_UNIT, "-1")],
)
def test_invalid_settings(key: str, value: str) -> None:
    with pytest.raises(WasteLookupError):
        validate_settings({**SETTINGS, key: value})


@pytest.mark.parametrize(
    "change",
    [
        {"msgCode": "error", "msg": "PRIVATE UPSTREAM MESSAGE"},
        {"msgCode": "unknown"},
        {"list": None},
        {"totalCnt": -1},
        {"totalCnt": True},
        {"paginationInfo": None},
        {"paginationInfo": {"totalPageCount": 1000}},
        {"list": [None, None]},
        {"list": [{"dttime": "bad", "qtyvalue": 1}, {}]},
        {"list": [{"dttime": "2025-12-31 12:00:00", "qtyvalue": 1}, {}]},
        {"list": [{"dttime": "2026-01-01 12:00:00", "qtyvalue": "NaN"}, {}]},
        {"list": [{"dttime": "2026-01-01 12:00:00", "qtyvalue": "-1"}, {}]},
        {"list": [{"dttime": "2026-01-01 12:00:00", "qtyvalue": "invalid"}, {}]},
        {"list": [{}, {}]},
    ],
)
def test_invalid_response_is_not_a_partial_total(change: dict[str, Any]) -> None:
    with pytest.raises(WasteError) as caught:
        parse_page({**payload(), **change}, MONTH, 1)
    assert "PRIVATE" not in str(caught.value)


def test_non_object_response() -> None:
    with pytest.raises(WasteError):
        parse_page([], MONTH, 1)


async def test_all_pages_summed_and_fixed_https_post(aresponses: Any) -> None:
    forms: list[dict[str, str]] = []

    async def response(request: Any) -> Any:
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        assert request.headers["Referer"].startswith("https://www.citywaste.or.kr/")
        form = dict(await request.post())
        forms.append(form)
        return aresponses.Response(
            text=json.dumps(payload(int(form["pageIndex"]), 12)),
            headers={"Content-Type": "application/json"},
        )

    aresponses.add("www.citywaste.or.kr", PATH, "post", response, repeat=2)
    async with ClientSession() as session:
        summary = await fetch_month(session, SETTINGS, MONTH, date(2026, 2, 1))
    assert summary == MonthSummary("2026-01", Decimal("1.80"), 12)
    assert [form["pageIndex"] for form in forms] == ["1", "2"]
    assert forms[0]["startchdate"] == "20260101"
    assert forms[0]["endchdate"] == "20260131"
    assert forms[0]["tagprintcd"] == SETTINGS[CONF_TAG]


async def test_empty_current_month_uses_today_and_returns_zero(aresponses: Any) -> None:
    async def response(request: Any) -> Any:
        form = await request.post()
        assert form["endchdate"] == "20260104"
        return aresponses.Response(text=json.dumps(payload(total=0)))

    aresponses.add("www.citywaste.or.kr", PATH, "post", response)
    async with ClientSession() as session:
        assert await fetch_month(session, SETTINGS, MONTH, date(2026, 1, 4)) == MonthSummary(
            "2026-01", Decimal(0), 0
        )


@pytest.mark.parametrize(
    ("status", "body"),
    [(302, ""), (403, "PRIVATE"), (500, ""), (200, "invalid json"), (200, " " * (MAX_BODY + 1))],
)
async def test_http_failures_bounded_and_redirects_blocked(
    aresponses: Any, status: int, body: str
) -> None:
    aresponses.add(
        "www.citywaste.or.kr",
        PATH,
        "post",
        aresponses.Response(
            status=status, text=body, headers={"Location": "https://unexpected.example/"}
        ),
    )
    async with ClientSession() as session:
        with pytest.raises(WasteError) as caught:
            await fetch_month(session, SETTINGS, MONTH, MONTH)
    assert SETTINGS[CONF_TAG] not in str(caught.value)


async def test_changed_total_cannot_publish_partial_month(aresponses: Any) -> None:
    for body in (payload(1, 12), payload(2, 13)):
        aresponses.add(
            "www.citywaste.or.kr", PATH, "post", aresponses.Response(text=json.dumps(body))
        )
    async with ClientSession() as session:
        with pytest.raises(WasteError, match="changed"):
            await fetch_month(session, SETTINGS, MONTH, MONTH)


async def test_network_error_sanitized() -> None:
    session = MagicMock()
    session.post.side_effect = TimeoutError("PRIVATE")
    with pytest.raises(WasteError, match="unavailable"):
        await fetch_month(session, SETTINGS, MONTH, MONTH)


async def test_query_month_selection_and_coordinator_failure(hass: Any) -> None:
    with (
        patch("custom_components.kepco_on.waste.async_get_clientsession"),
        patch(
            "custom_components.kepco_on.waste.fetch_month", new=AsyncMock(side_effect=SUMMARIES)
        ) as fetch,
    ):
        assert await async_query(hass, SETTINGS) == SUMMARIES
    first, second = [call.args[2] for call in fetch.call_args_list]
    assert second == previous_month(first)
    coordinator = WasteCoordinator(hass, entry())
    with patch(
        "custom_components.kepco_on.waste.async_query", new=AsyncMock(return_value=SUMMARIES)
    ):
        assert await coordinator._async_update_data() == SUMMARIES
    with (
        patch(
            "custom_components.kepco_on.waste.async_query",
            new=AsyncMock(side_effect=WasteError("PRIVATE")),
        ),
        pytest.raises(UpdateFailed, match="RFID monthly"),
    ):
        await coordinator._async_update_data()


async def test_setup_sensor_values_unload_and_private_diagnostics(
    hass: Any, enable_custom_integrations: None
) -> None:

    # Use a deterministic date for monthly summaries.
    with patch(
        "custom_components.kepco_on.waste_sensor.dt_util.utcnow",
        return_value=datetime(2026, 1, 4, tzinfo=UTC),
    ):
        await check_setup_sensor_values_unload_and_private_diagnostics(hass)


async def check_setup_sensor_values_unload_and_private_diagnostics(hass: Any) -> None:
    from custom_components.kepco_on.diagnostics import async_get_config_entry_diagnostics
    from homeassistant.helpers import entity_registry as er

    config = entry()
    config.add_to_hass(hass)
    with patch(
        "custom_components.kepco_on.waste.async_query", new=AsyncMock(return_value=SUMMARIES)
    ):
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
    entities = er.async_entries_for_config_entry(er.async_get(hass), config.entry_id)
    assert len(entities) == 4
    assert sorted(hass.states.get(entity.entity_id).state for entity in entities) == [
        "0",
        "0",
        "1.25",
        "2",
    ]
    diagnostics = await async_get_config_entry_diagnostics(hass, config)
    assert diagnostics["service"] == SERVICE
    assert SETTINGS[CONF_TAG] not in json.dumps(diagnostics)
    assert "999" not in json.dumps(diagnostics)
    sensor = WasteSensor(config, 0, False)
    assert sensor.extra_state_attributes == {"month": "2026-01", "lookup_status": "ok"}
    empty = WasteSensor(config, 1, True)
    assert empty.extra_state_attributes["lookup_status"] == "no_records"
    with patch(
        "custom_components.kepco_on.waste_sensor.dt_util.utcnow",
        return_value=datetime(2026, 2, 1, tzinfo=UTC),
    ):
        assert sensor.native_value is None
        assert sensor.extra_state_attributes["lookup_status"] == "stale_month"
        registered_id = next(
            item.entity_id for item in entities if item.unique_id == sensor.unique_id
        )
        registered = hass.data["entity_components"]["sensor"].get_entity(registered_id)
        registered.async_month_rollover(datetime(2026, 2, 1, tzinfo=UTC))
        assert hass.states.get(registered_id).state == "unknown"
    config.runtime_data.async_set_updated_data(())
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}
    assert await hass.config_entries.async_unload(config.entry_id)
    await hass.async_block_till_done()


async def test_flow_and_options(hass: Any) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow
    from custom_components.kepco_on.waste_flow import WasteOptionsFlow

    from tests.test_config_flow import make_flow

    flow = make_flow()
    assert (await flow.async_step_waste())["step_id"] == "waste"
    with patch(
        "custom_components.kepco_on.waste_flow.async_query", new=AsyncMock(return_value=SUMMARIES)
    ):
        result = await flow.async_step_waste(SETTINGS)
    assert result["type"] == "create_entry"
    assert result["data"] == SETTINGS
    assert SETTINGS[CONF_TAG] not in result["title"]
    config = entry()
    config.add_to_hass(hass)
    options = KepcoOnConfigFlow.async_get_options_flow(config)
    assert isinstance(options, WasteOptionsFlow)
    options.hass, options.handler = hass, config.entry_id
    assert (await options.async_step_init())["step_id"] == "waste_options"
    assert (await options.async_step_waste_options({"waste_interval_hours": "48"}))["data"] == {
        "waste_interval_hours": 48
    }


async def test_empty_months_allow_setup() -> None:
    from tests.test_config_flow import make_flow

    empty = tuple(MonthSummary(month.month, Decimal(0), 0) for month in SUMMARIES)
    with patch(
        "custom_components.kepco_on.waste_flow.async_query", new=AsyncMock(return_value=empty)
    ):
        result = await make_flow().async_step_waste(SETTINGS)
    assert result["type"] == "create_entry"
    assert result["data"] == SETTINGS


async def test_manual_update_refreshes_all_month_sensors(
    hass: Any, enable_custom_integrations: None
) -> None:
    from homeassistant.helpers import entity_registry as er
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "homeassistant", {})
    config = entry()
    config.add_to_hass(hass)
    empty = tuple(MonthSummary(month.month, Decimal(0), 0) for month in SUMMARIES)
    with (
        patch(
            "custom_components.kepco_on.waste_sensor.dt_util.utcnow",
            return_value=datetime(2026, 1, 4, tzinfo=UTC),
        ),
        patch(
            "custom_components.kepco_on.waste.async_query",
            new=AsyncMock(side_effect=[empty, SUMMARIES]),
        ) as query,
    ):
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
        entities = er.async_entries_for_config_entry(er.async_get(hass), config.entry_id)
        assert len(entities) == 4
        assert all(hass.states.get(item.entity_id).state == "0" for item in entities)
        assert all(
            hass.states.get(item.entity_id).attributes["lookup_status"] == "no_records"
            for item in entities
        )
        await hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": entities[0].entity_id},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert query.await_count == 2
        assert sorted(hass.states.get(item.entity_id).state for item in entities) == [
            "0",
            "0",
            "1.25",
            "2",
        ]
        query.side_effect = WasteError("PRIVATE")
        await config.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert all(hass.states.get(item.entity_id).state == "unavailable" for item in entities)
        assert await hass.config_entries.async_unload(config.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (WasteError("PRIVATE"), "waste_cannot_connect"),
        (WasteLookupError("PRIVATE"), "waste_invalid_lookup"),
    ],
)
async def test_flow_errors(response: Any, error: str) -> None:
    from tests.test_config_flow import make_flow

    mock = (
        AsyncMock(side_effect=response)
        if isinstance(response, Exception)
        else AsyncMock(return_value=response)
    )
    with patch("custom_components.kepco_on.waste_flow.async_query", new=mock):
        assert (await make_flow().async_step_waste(SETTINGS))["errors"] == {"base": error}


@pytest.mark.parametrize("source", ["reconfigure", "reauth"])
async def test_flow_same_identity_reconfigure(source: str) -> None:
    from tests.test_config_flow import FakeConfigEntry, make_flow

    flow = make_flow()
    config = FakeConfigEntry(data=SETTINGS, unique_id=settings_id(SETTINGS))
    flow.hass.config_entries.entries_by_id[config.entry_id] = config
    flow.hass.config_entries.async_reload = AsyncMock()
    flow.context = {"source": source, "entry_id": config.entry_id}
    if source == "reconfigure":
        result = await flow.async_step_reconfigure()
    else:
        result = await flow.async_step_reauth(SETTINGS)
    assert result["step_id"] == "waste"
    with patch(
        "custom_components.kepco_on.waste_flow.async_query", new=AsyncMock(return_value=SUMMARIES)
    ):
        assert (await flow.async_step_waste(SETTINGS))["reason"] == "reconfigure_successful"
    assert (await flow.async_step_waste({**SETTINGS, CONF_UNIT: "777"}))[
        "reason"
    ] == "waste_wrong_household"
