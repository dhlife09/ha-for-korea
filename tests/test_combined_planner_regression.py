"""Synthetic, contract-scoped regressions; no live account data or requests."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest
from custom_components.kepco_on.api import KepcoOnClient
from custom_components.kepco_on.const import VERSION
from custom_components.kepco_on.parser import parse_power_planner

from .test_apartment_planner import PlannerAuth, customer
from .test_sensor import bill as synthetic_bill
from .test_sensor import by_key, setup_entities


@pytest.mark.parametrize("value", [0, "0", 246800, "246800", "246,800", 1250.5, "0.5"])
def test_wh_conversion_is_explicit_and_does_not_infer_from_magnitude(value: object) -> None:
    payload: dict[str, object] = {
        "dma_powerPlanner": {"RETURN_CD": "00", "F_AP_QT": value, "PREDICT_TOT": "987650"}
    }
    raw = float(str(value).replace(",", ""))
    assert parse_power_planner(payload) == (raw, None)
    converted, predicted = parse_power_planner(payload, current_unit_wh=True)
    assert converted == pytest.approx(raw / 1000)
    assert predicted is None


@pytest.mark.parametrize("value", [None, "", "null"])
def test_wh_profile_never_fabricates_missing_usage(value: object) -> None:
    assert parse_power_planner({"dma_powerPlanner": {"F_AP_QT": value}}, current_unit_wh=True) == (
        None,
        None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contract", "expected", "divisor", "variant"),
    [
        ("아파트(종합계약)", 246.8, 1000, "apartment_customer_and_contract"),
        ("아파트(단일계약)", 246800.0, 1, "household_and_change_date"),
        ("아파트(종합계약/나)", 246800.0, 1, "household_and_change_date"),
    ],
)
async def test_parser_to_sensor_converts_once_and_keeps_diagnostic_aliases(
    contract: str, expected: float, divisor: int, variant: str
) -> None:
    item = customer(contract)
    auth = PlannerAuth(
        {"dma_powerPlanner": {"RETURN_CD": "00", "F_AP_QT": "246800", "PREDICT_TOT": "987650"}}
    )
    with patch("custom_components.kepco_on.api.parse_bill", return_value=synthetic_bill()):
        bill = await KepcoOnClient(cast("Any", auth)).async_get_bill(item)
    sensors = by_key(
        await setup_entities(customers=(item,), bills_by_customer_key={item.stable_key: bill})
    )
    current = sensors["current_period_usage"]
    predicted = sensors["predicted_period_usage"]
    assert current.native_value == pytest.approx(expected)
    assert current.available is True
    combined = contract == "아파트(종합계약)"
    if combined:
        assert predicted.native_value == pytest.approx(987.65)
    else:
        assert predicted.native_value is None
    assert predicted.available is True
    assert predicted.extra_state_attributes["data_status"] == (
        "ok" if combined else "source_unit_unverified"
    )
    assert current.extra_state_attributes["value_divisor"] == divisor
    assert predicted.extra_state_attributes["value_divisor"] == (1000 if combined else None)
    for sensor in (current, predicted):
        attrs = sensor.extra_state_attributes
        assert attrs["return_code"] == attrs["provider_return_code"] == "00"
        assert attrs["integration_version"] == VERSION == "1.3.1"
        assert attrs["request_variant"] == variant
        assert "TEST_BUILDING" not in str(attrs)
        assert "TEST_HOUSEHOLD" not in str(attrs)
    assert sensors["monthly_usage"].native_value == synthetic_bill().usage_kwh
    assert len(auth.calls) == 2


@pytest.mark.asyncio
async def test_invalid_energy_preserves_safe_code_and_does_not_mask_failure() -> None:
    item = customer()
    auth = PlannerAuth({"dma_powerPlanner": {"RETURN_CD": "00", "F_AP_QT": "NaN"}})
    with patch("custom_components.kepco_on.api.parse_bill", return_value=synthetic_bill()):
        bill = await KepcoOnClient(cast("Any", auth)).async_get_bill(item)
    assert bill.power_planner_status == "invalid_response"
    assert bill.power_planner_return_code == "00"
    sensors = by_key(
        await setup_entities(customers=(item,), bills_by_customer_key={item.stable_key: bill})
    )
    for key in ("current_period_usage", "predicted_period_usage"):
        assert sensors[key].available is True
        assert sensors[key].extra_state_attributes["data_status"] == (
            "invalid_response" if key == "current_period_usage" else "no_data"
        )
        assert sensors[key].extra_state_attributes["provider_return_code"] == "00"
    assert sensors["monthly_usage"].available is True
