"""Published holiday dates, exact store identity, transport limits and HA lifecycle."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession
from custom_components.kepco_on.const import DOMAIN
from custom_components.kepco_on.mart import (
    async_catalog,
    async_query,
    manual_dates,
    today_in_korea,
)
from custom_components.kepco_on.mart_api import (
    MAX_BODY,
    HolidaySchedule,
    MartError,
    Shop,
    checked_text,
    fetch_catalog,
    fetch_lotte_holidays,
    month_key,
    next_month,
    parse_catalog,
    parse_dates,
    request_json,
)
from custom_components.kepco_on.mart_flow import MartOptionsFlow, settings_id
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytestmark = pytest.mark.usefixtures("socket_enabled")
DAY = date(2026, 1, 4)
SHOP = Shop("TEST000", "테스트지점", "emart", holidays=(date(2026, 1, 11), date(2026, 1, 25)))
LOTTE = Shop(
    "TEST001",
    "테스트슈퍼",
    "lottesuper",
    (("trNo", "TESTTR"), ("lrtrNo", "TESTLR"), ("oflnAfflCd", "LTFS"), ("strKndCd", "31")),
)
DATA = {
    "service": "mart_holidays",
    "brand": "emart",
    "store_id": SHOP.id,
    "store_name": SHOP.name,
    "manual_dates": [],
}
SCHEDULE = HolidaySchedule(SHOP.holidays, frozenset({"2026-01"}))


def emart_row(**changes: Any) -> dict[str, Any]:
    return {
        "ID": SHOP.id,
        "NAME": SHOP.name,
        "HOLIDAY_DAY1_YYYYMMDD": "20260111",
        "HOLIDAY_DAY2_YYYYMMDD": "20260125",
        **changes,
    }


def lotte_row(**changes: Any) -> dict[str, Any]:
    return {"sstrCd": LOTTE.id, "strNm": LOTTE.name, **dict(LOTTE.lookup), **changes}


def test_parse_public_fields_only_and_brands() -> None:
    shops = parse_catalog(
        {
            "dataList": [
                emart_row(ADDRESS1="PRIVATE", MODIFY_ADMIN_ID="PRIVATE"),
                emart_row(ID="TEST002", NAME="에브리데이 테스트"),
                emart_row(ID="TEST003", NAME="노브랜드 테스트"),
            ]
        },
        "emart",
        DAY,
    )
    assert shops[0] == SHOP
    assert [shop.brand for shop in shops] == ["emart", "everyday", "nobrand"]
    assert "PRIVATE" not in str(shops)
    assert parse_catalog({"storeInfo": [lotte_row()]}, "lottesuper", DAY) == (LOTTE,)
    assert parse_catalog({"storeInfo": [lotte_row(strNm="TRU테스트")]}, "lottemart", DAY) == ()
    assert parse_catalog({"storeInfo": [lotte_row(lrtrNo="")]}, "lottesuper", DAY)[0].lookup == ()
    assert month_key(DAY) == "2026-01"
    assert next_month(date(2026, 12, 4)) == date(2027, 1, 1)
    assert parse_dates([None, "", "20260111", "20260111"], DAY) == (date(2026, 1, 11),)
    assert manual_dates("2026-01-25,\n2026-01-11;2026-01-11") == ["2026-01-11", "2026-01-25"]
    assert manual_dates("") == []
    assert checked_text(" Name ") == "Name"


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"dataList": []},
        {"dataList": [None]},
        {"dataList": [emart_row(), emart_row()]},
        {"dataList": [emart_row(ID="../../bad")]},
        {"dataList": [emart_row(NAME="bad\nname")]},
        {"dataList": [emart_row(NAME="a" * 101)]},
    ],
)
def test_invalid_catalog(value: Any) -> None:
    with pytest.raises(MartError):
        parse_catalog(value, "emart", DAY)


@pytest.mark.parametrize("value", ["2026-01-11", "20260230", "20260211", "NaN"])
def test_invalid_official_date(value: str) -> None:
    with pytest.raises(MartError):
        parse_dates([value], DAY)


@pytest.mark.parametrize("value", ["bad", ",".join(["2026-01-11"] * 101), "9999-01-01"])
def test_invalid_manual_date(value: str) -> None:
    with pytest.raises(ValueError, match=r"Invalid|Too many"):
        manual_dates(value)


async def test_http_catalog_post_201_and_lotte_get(aresponses: Any) -> None:
    async def emart(request: Any) -> Any:
        assert request.method == "POST"
        assert (await request.post())["month"] == "1"
        return aresponses.Response(status=201, text=json.dumps({"dataList": [emart_row()]}))

    aresponses.add("store.emart.com", "/branch/searchList.do", "post", emart)
    aresponses.add(
        "www.lotteon.com",
        "/p/lotteplus/offlinestore/selectStoreSearchList",
        "get",
        aresponses.Response(text=json.dumps({"storeInfo": [lotte_row()]})),
        repeat=2,
    )
    aresponses.add(
        "www.lotteon.com",
        "/p/lotteplus/holiday/selectHolidayList",
        "get",
        aresponses.Response(text=json.dumps({"holidayList": [{"holidayDate": "20260111"}]})),
    )
    async with ClientSession() as session:
        assert await fetch_catalog(session, "emart", DAY) == (SHOP,)
        assert await fetch_catalog(session, "lottesuper", DAY) == (LOTTE,)
        assert (await fetch_catalog(session, "lottemart", DAY))[0].id == LOTTE.id
        assert await fetch_lotte_holidays(session, LOTTE) == (date(2026, 1, 11),)
        assert await fetch_lotte_holidays(session, SHOP) == ()
        with pytest.raises(MartError):
            await fetch_catalog(session, "unknown", DAY)


@pytest.mark.parametrize(
    "body", ["[]", "{}", '{"holidayList":[null]}', json.dumps({"holidayList": [{}] * 101})]
)
async def test_invalid_holiday_response(aresponses: Any, body: str) -> None:
    aresponses.add(
        "www.lotteon.com",
        "/p/lotteplus/holiday/selectHolidayList",
        "get",
        aresponses.Response(text=body),
    )
    async with ClientSession() as session:
        with pytest.raises(MartError):
            await fetch_lotte_holidays(session, LOTTE)


@pytest.mark.parametrize(
    ("status", "body"),
    [(302, ""), (403, "PRIVATE"), (200, "bad json"), (200, " " * (MAX_BODY + 1))],
)
async def test_safe_transport_failures(aresponses: Any, status: int, body: str) -> None:
    aresponses.add(
        "store.emart.com",
        "/branch/searchList.do",
        "post",
        aresponses.Response(
            status=status, text=body, headers={"Location": "https://untrusted.example/"}
        ),
    )
    async with ClientSession() as session:
        with pytest.raises(MartError) as caught:
            await fetch_catalog(session, "emart", DAY)
    assert "PRIVATE" not in str(caught.value)


async def test_network_error_sanitized() -> None:
    session = MagicMock()
    session.request.side_effect = TimeoutError("PRIVATE")
    with pytest.raises(MartError, match="unavailable"):
        await request_json(session, "https://store.emart.com/branch/searchList.do")


async def test_cache_expiry_and_exact_identity_query(hass: Any) -> None:
    with patch(
        "custom_components.kepco_on.mart.fetch_catalog", new=AsyncMock(return_value=(SHOP,))
    ) as fetch:
        assert await async_catalog(hass, "emart", DAY) == (SHOP,)
        assert await async_catalog(hass, "everyday", DAY) == (SHOP,)
        assert fetch.await_count == 1
        with patch(
            "custom_components.kepco_on.mart.dt_util.utcnow",
            return_value=datetime.now(UTC) + timedelta(hours=7),
        ):
            assert await async_catalog(hass, "emart", DAY) == (SHOP,)
        assert fetch.await_count == 2
    with (
        patch("custom_components.kepco_on.mart.async_catalog", new=AsyncMock(return_value=(SHOP,))),
        patch("custom_components.kepco_on.mart.today_in_korea", return_value=DAY),
    ):
        assert await async_query(hass, DATA, {}) == SCHEDULE
        with pytest.raises(MartError, match="no longer"):
            await async_query(hass, {**DATA, "store_id": "OTHER"}, {})
    with (
        patch(
            "custom_components.kepco_on.mart.async_catalog", new=AsyncMock(return_value=(LOTTE,))
        ),
        patch(
            "custom_components.kepco_on.mart.fetch_lotte_holidays", new=AsyncMock(return_value=())
        ),
    ):
        assert await async_query(
            hass, {**DATA, "brand": "lottesuper", "store_id": LOTTE.id}, {}
        ) == HolidaySchedule((), frozenset())
    assert (await async_query(hass, DATA, {"manual_dates": ["2026-01-11"]})).source == "manual"
    with pytest.raises(MartError):
        await async_query(hass, {**DATA, "brand": "unknown"}, {})
    assert isinstance(today_in_korea(), date)


async def test_lifecycle_midnight_unknown_manual_and_diagnostics(
    hass: Any, enable_custom_integrations: None
) -> None:
    from custom_components.kepco_on.diagnostics import async_get_config_entry_diagnostics
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(domain=DOMAIN, version=3, data=DATA, unique_id=settings_id(SHOP))
    entry.add_to_hass(hass)
    with (
        patch("custom_components.kepco_on.mart.async_query", new=AsyncMock(return_value=SCHEDULE)),
        patch("custom_components.kepco_on.mart.today_in_korea", return_value=DAY),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entities) == 3
    assert {hass.states.get(item.entity_id).state for item in entities} == {
        "7",
        "2026-01-11",
        "오늘 정기휴무일 아님",
    }
    sensors = {
        item.unique_id.rsplit("_", 1)[1]: hass.data["entity_components"]["sensor"].get_entity(
            item.entity_id
        )
        for item in entities
    }
    with patch("custom_components.kepco_on.mart.today_in_korea", return_value=date(2026, 1, 11)):
        sensors["status"].async_midnight(datetime.now(UTC))
        assert sensors["status"].native_value == "오늘 휴무"
        assert sensors["days"].native_value == 0
    with patch("custom_components.kepco_on.mart.today_in_korea", return_value=date(2026, 2, 1)):
        assert sensors["status"].native_value == "휴무일 미게시"
        assert sensors["next"].native_value is None
        assert sensors["days"].native_value is None
    with patch("custom_components.kepco_on.mart.today_in_korea", return_value=DAY):
        entry.runtime_data.async_set_updated_data(
            HolidaySchedule(SHOP.holidays, SCHEDULE.months, "manual")
        )
        assert sensors["status"].native_value == "등록된 휴무일 아님"
    assert sensors["status"].extra_state_attributes["schedule_source"] == "manual"
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert SHOP.name not in str(diagnostics)
    assert SHOP.id not in str(diagnostics)
    with (
        patch(
            "custom_components.kepco_on.mart.async_query",
            new=AsyncMock(side_effect=MartError("PRIVATE")),
        ),
        pytest.raises(UpdateFailed),
    ):
        await entry.runtime_data._async_update_data()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_search_store_options_and_reconfigure(hass: Any) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow

    from tests.test_config_flow import FakeConfigEntry, make_flow

    flow = make_flow()
    assert (await flow.async_step_mart())["step_id"] == "mart"
    assert (await flow.async_step_mart_store())["reason"] == "mart_search_again"
    with patch(
        "custom_components.kepco_on.mart_flow.async_catalog", new=AsyncMock(return_value=(SHOP,))
    ):
        assert (await flow.async_step_mart({"brand": "emart", "search": "none"}))["errors"]
        assert (await flow.async_step_mart({"brand": "emart", "search": "테스트"}))[
            "step_id"
        ] == "mart_store"
    assert (await flow.async_step_mart_store())["step_id"] == "mart_store"
    assert (await flow.async_step_mart_store({"store_id": "unknown"}))["errors"]
    with patch(
        "custom_components.kepco_on.mart_flow.async_query", new=AsyncMock(return_value=SCHEDULE)
    ):
        result = await flow.async_step_mart_store({"store_id": SHOP.id})
    assert result["data"] == DATA
    entry = MockConfigEntry(domain=DOMAIN, version=3, data=DATA, unique_id=settings_id(SHOP))
    entry.add_to_hass(hass)
    options = KepcoOnConfigFlow.async_get_options_flow(entry)
    assert isinstance(options, MartOptionsFlow)
    options.hass, options.handler = hass, entry.entry_id
    assert (await options.async_step_init())["step_id"] == "mart_options"
    assert (await options.async_step_mart_options({"manual_dates": "bad"}))["errors"]
    assert (await options.async_step_mart_options({"mart_interval_hours": "1"}))["errors"]
    result = await options.async_step_mart_options(
        {"manual_dates": "2026-01-11", "mart_interval_hours": "24"}
    )
    assert result["data"] == {"manual_dates": ["2026-01-11"], "mart_interval_hours": 24}
    fake = FakeConfigEntry(data=DATA, unique_id=settings_id(SHOP))
    flow.hass.config_entries.entries_by_id[fake.entry_id] = fake
    flow.hass.config_entries.async_reload = AsyncMock()
    flow.context = {"source": "reconfigure", "entry_id": fake.entry_id}
    assert (await flow.async_step_reconfigure())["step_id"] == "mart"
    with (
        patch(
            "custom_components.kepco_on.mart_flow.async_catalog",
            new=AsyncMock(return_value=(SHOP,)),
        ),
        patch(
            "custom_components.kepco_on.mart_flow.async_query", new=AsyncMock(return_value=SCHEDULE)
        ),
    ):
        await flow.async_step_mart({"brand": "emart"})
        assert (await flow.async_step_mart_store({"store_id": SHOP.id}))[
            "reason"
        ] == "reconfigure_successful"
    flow.context["source"] = "reauth"
    assert (await flow.async_step_reauth(DATA))["step_id"] == "mart"


async def test_flow_errors_and_changed_store() -> None:
    from tests.test_config_flow import FakeConfigEntry, make_flow

    flow = make_flow()
    assert (await flow.async_step_mart({"brand": "invalid"}))["errors"]
    with patch(
        "custom_components.kepco_on.mart_flow.async_catalog",
        new=AsyncMock(side_effect=MartError("PRIVATE")),
    ):
        assert (await flow.async_step_mart({"brand": "emart"}))["errors"]
    flow._mart_choices = {SHOP.id: SHOP}
    with patch(
        "custom_components.kepco_on.mart_flow.async_query",
        new=AsyncMock(side_effect=MartError("PRIVATE")),
    ):
        assert (await flow.async_step_mart_store({"store_id": SHOP.id}))["errors"] == {
            "base": "mart_cannot_connect"
        }
    config = FakeConfigEntry(data=DATA, unique_id="another")
    flow.hass.config_entries.entries_by_id[config.entry_id] = config
    flow.context = {"source": "reconfigure", "entry_id": config.entry_id}
    assert (await flow.async_step_mart_store({"store_id": SHOP.id}))["reason"] == "mart_wrong_store"
