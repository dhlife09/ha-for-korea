"""Synthetic end-to-end regressions for the explicit combined-contract profile."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from custom_components.kepco_on.api import KepcoOnClient
from custom_components.kepco_on.const import ENDPOINT_APT_BILL_DETAIL, ENDPOINT_POWER_PLANNER
from custom_components.kepco_on.exceptions import (
    KepcoOnAuthError,
    KepcoOnConnectionError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
    KepcoOnSessionExpired,
)
from custom_components.kepco_on.models import KepcoBill
from custom_components.kepco_on.parser import parse_power_planner, parse_power_planner_value
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

from .test_apartment_planner import customer
from .test_planner_state_publication import publish
from .test_sensor import by_key, setup_entities

FIELDS = ("F_AP_QT", "PREDICT_TOT")
KEYS = ("current_period_usage", "predicted_period_usage")


class FullAuth:
    """Only the transport is faked: bill/parser/API/entities are production code."""

    def __init__(self, planner: dict[str, object] | BaseException) -> None:
        self.planner = planner
        self.calls: list[tuple[str, dict[str, object] | None, str | None]] = []

    async def async_protected_request(
        self,
        path: str,
        payload: dict[str, object] | None,
        *,
        submission_id: str | None = None,
    ) -> dict[str, object]:
        self.calls.append((path, payload, submission_id))
        if path == ENDPOINT_APT_BILL_DETAIL:
            return cast(
                "dict[str, object]",
                json.loads(
                    (Path(__file__).parent / "fixtures/bill_latest.json").read_text(
                        encoding="utf-8"
                    )
                ),
            )
        assert path == ENDPOINT_POWER_PLANNER
        if isinstance(self.planner, BaseException):
            raise self.planner
        return self.planner


async def fetch(planner: dict[str, object] | BaseException) -> tuple[KepcoBill, FullAuth]:
    auth = FullAuth(planner)
    return await KepcoOnClient(cast("Any", auth)).async_get_bill(customer()), auth


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [0, "0", "0.000", 12345, "12,345", 0.5, "246800.123"])
def test_field_scaling_is_explicit_and_preserves_precision(field: str, value: object) -> None:
    payload: dict[str, object] = {"dma_powerPlanner": {field: value, "RETURN_CD": "00"}}
    expected = float(str(value).replace(",", ""))
    assert parse_power_planner_value(payload, field) == pytest.approx(expected)
    assert parse_power_planner_value(payload, field, unit_wh=True) == pytest.approx(expected / 1000)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [None, "", "null", " NULL "])
def test_missing_values_remain_none(field: str, value: object) -> None:
    payload: dict[str, object] = {"dma_powerPlanner": {field: value}}
    assert parse_power_planner_value(payload, field, unit_wh=True) is None


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize(
    "value", [True, False, -1, "NaN", "Infinity", "-Infinity", "bad", [], {}, 10**400]
)
def test_invalid_numbers_are_rejected_safely(field: str, value: object) -> None:
    payload: dict[str, object] = {"dma_powerPlanner": {field: value}}
    with pytest.raises(KepcoOnProtocolError):
        parse_power_planner_value(payload, field, unit_wh=True)


def test_legacy_tuple_requires_explicit_prediction_profile() -> None:
    payload: dict[str, object] = {"dma_powerPlanner": {"F_AP_QT": 246800, "PREDICT_TOT": 987654}}
    assert parse_power_planner(payload) == (246800.0, None)
    assert parse_power_planner(payload, current_unit_wh=True) == (246.8, None)
    current, predicted = parse_power_planner(payload, current_unit_wh=True, predicted_unit_wh=True)
    assert current == pytest.approx(246.8)
    assert predicted == pytest.approx(987.654)
    with pytest.raises(ValueError, match="Unsupported Power Planner field"):
        parse_power_planner_value(payload, "PRIVATE_FIELD")


@pytest.mark.parametrize("code", [None, "", "00"])
async def test_real_parsers_publish_both_values_to_ha_and_template(
    hass: HomeAssistant, code: str | None
) -> None:
    bill, auth = await fetch(
        {"dma_powerPlanner": {"RETURN_CD": code, "F_AP_QT": "246800", "PREDICT_TOT": "987654"}}
    )
    item = customer()
    sensors = await publish(
        hass, await setup_entities(customers=(item,), bills_by_customer_key={item.stable_key: bill})
    )
    for key, field, expected in zip(KEYS, FIELDS, (246.8, 987.654), strict=True):
        state = hass.states.get(sensors[key].entity_id)
        assert state is not None
        assert float(state.state) == pytest.approx(expected)
        assert state.attributes["source_field"] == field
        assert state.attributes["data_status"] == "ok"
        assert state.attributes["value_divisor"] == 1000
        assert state.attributes["conversion_basis"] == "user_reported_combined_contract"
        assert state.attributes["integration_version"] == "1.0.0"
        assert state.attributes["provider_return_code"] == (code or None)
        assert state.attributes["unit_of_measurement"] == "kWh"
        assert sensors[key].entity_description.suggested_display_precision == 2
        assert sensors[key].unique_id == f"{item.stable_key}_{key}"
        assert "TEST_BUILDING" not in str(state.attributes)
        assert "TEST_HOUSEHOLD" not in str(state.attributes)
    planner_search = cast("dict[str, object]", auth.calls[1][1])["dma_search"]
    assert planner_search == {
        "schYm": "",
        "custNo": "TEST_BUILDING",
        "gubun": "",
        "schChart": "12",
        "CUST_NO": "",
        "housCntrNo": "TEST_HOUSEHOLD",
        "yyyymm": "",
        "searchType": "",
        "dong": "",
        "ho": "",
        "months": "",
        "chgYmd": "",
    }
    assert auth.calls[1][2] == "mf_wfm_layout_sbm_powerPlanner"
    assert len(auth.calls) == 2
    monthly = hass.states.get(sensors["monthly_usage"].entity_id)
    assert monthly is not None
    assert monthly.state == str(bill.usage_kwh)
    output = Template(
        "{% for s in states.sensor if s.attributes.get('source_field') "
        "in ['F_AP_QT','PREDICT_TOT'] %}{{ s.state }};{% endfor %}",
        hass,
    ).async_render(parse_result=False)
    assert "246.8" in output
    assert "987.654" in output
    assert "unknown" not in output


@pytest.mark.parametrize("broken_field", FIELDS)
@pytest.mark.parametrize(
    ("bad", "status"), [(None, "no_data"), ("NaN", "invalid_response"), (True, "invalid_response")]
)
async def test_bad_field_never_discards_other_valid_value(
    hass: HomeAssistant, broken_field: str, bad: object, status: str
) -> None:
    values: dict[str, object] = {"F_AP_QT": 246800, "PREDICT_TOT": 987654, "RETURN_CD": "00"}
    values[broken_field] = bad
    bill, _ = await fetch({"dma_powerPlanner": values})
    item = customer()
    sensors = await publish(
        hass, await setup_entities(customers=(item,), bills_by_customer_key={item.stable_key: bill})
    )
    for key, field, expected in zip(KEYS, FIELDS, (246.8, 987.654), strict=True):
        state = hass.states.get(sensors[key].entity_id)
        assert state is not None
        assert state.attributes["provider_return_code"] == "00"
        if field == broken_field:
            assert state.state == STATE_UNKNOWN
            assert state.attributes["data_status"] == status
        else:
            assert float(state.state) == pytest.approx(expected)
            assert state.attributes["data_status"] == "ok"
    assert sensors["monthly_usage"].native_value == bill.usage_kwh


@pytest.mark.parametrize("code", ["90", "01", "99"])
async def test_provider_failure_never_turns_into_usable_values(code: str) -> None:
    bill, auth = await fetch(
        {"dma_powerPlanner": {"RETURN_CD": code, "F_AP_QT": 246800, "PREDICT_TOT": 987654}}
    )
    assert bill.current_period_usage_kwh is None
    assert bill.predicted_period_usage_kwh is None
    assert bill.power_planner_current_status == bill.power_planner_prediction_status == "no_data"
    assert bill.power_planner_return_code == code
    assert len(auth.calls) == 2


async def test_zero_missing_then_recovery_uses_latest_snapshot(hass: HomeAssistant) -> None:
    bill, _ = await fetch({"dma_powerPlanner": {"F_AP_QT": 0, "PREDICT_TOT": 0}})
    item = customer()
    entities = await setup_entities(
        customers=(item,), bills_by_customer_key={item.stable_key: bill}
    )
    sensors = await publish(hass, entities)
    for key in KEYS:
        state = hass.states.get(sensors[key].entity_id)
        assert state is not None
        assert float(state.state) == 0
    coordinator = sensors[KEYS[0]].coordinator
    scenarios: tuple[tuple[dict[str, object], tuple[float, float] | None], ...] = (
        ({"dma_powerPlanner": {}}, None),
        ({"dma_powerPlanner": {"F_AP_QT": 1250, "PREDICT_TOT": 2500}}, (1.25, 2.5)),
    )
    for response, expected in scenarios:
        next_bill, _ = await fetch(response)
        coordinator.data = replace(
            coordinator.data, bills_by_customer_key={item.stable_key: next_bill}
        )
        for index, key in enumerate(KEYS):
            sensors[key].async_write_ha_state()
            state = hass.states.get(sensors[key].entity_id)
            assert state is not None
            if expected is None:
                assert state.state == STATE_UNKNOWN
                assert state.attributes["data_status"] == "no_data"
            else:
                assert float(state.state) == expected[index]
                assert state.attributes["data_status"] == "ok"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (KepcoOnConnectionError("PRIVATE_CANARY"), "connection_error"),
        (KepcoOnRateLimitError("PRIVATE_CANARY"), "rate_limited"),
        (KepcoOnProtocolError("PRIVATE_CANARY"), "invalid_response"),
    ],
)
async def test_optional_transport_failure_keeps_bill_and_safe_status(
    error: Exception, status: str
) -> None:
    bill, _ = await fetch(error)
    item = customer()
    sensors = by_key(
        await setup_entities(customers=(item,), bills_by_customer_key={item.stable_key: bill})
    )
    assert sensors["monthly_usage"].available is True
    for key in KEYS:
        assert sensors[key].native_value is None
        assert sensors[key].extra_state_attributes["data_status"] == status
        assert "PRIVATE_CANARY" not in str(sensors[key].extra_state_attributes)


@pytest.mark.parametrize(
    "error",
    [KepcoOnAuthError("expired"), KepcoOnSessionExpired("expired"), asyncio.CancelledError()],
)
async def test_auth_and_cancellation_still_propagate(error: BaseException) -> None:
    with pytest.raises(type(error)):
        await fetch(error)
