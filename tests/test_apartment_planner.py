"""Synthetic regressions for apartment contracts and optional planner data."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, cast
from unittest.mock import patch

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
from custom_components.kepco_on.models import (
    KepcoBill,
    KepcoCustomer,
    deserialize_customer,
    serialize_customer,
)
from custom_components.kepco_on.parser import parse_customers, parse_power_planner

from .test_sensor import bill as synthetic_bill
from .test_sensor import by_key, setup_entities

CONTRACTS = (
    "아파트(단일계약)",
    "아파트(종합계약)",
    "아파트(종합계약/나)",
    "오피스텔(종합계약)",
)


def customer_payload(contract: str) -> dict[str, object]:
    """Use distinct synthetic building and household IDs to prevent ID swaps."""
    return {
        "dlt_myPageAppendList": [
            {
                "CUST_NO": "TEST_BUILDING",
                "SI_CUST_NO": "TEST_HOUSEHOLD",
                "cntrMthdCd": contract,
                "APT_DONGNO": "0101",
                "APT_HONO": "0101",
                "DC_USER_CHG_NM_YMD": "20260409",
            }
        ]
    }


def customer(contract: str = "아파트(종합계약)") -> KepcoCustomer:
    return parse_customers(customer_payload(contract), "TEST_ACCOUNT_HASH")[0]


class PlannerAuth:
    """Fake the authenticated transport, never send requests to KEPCO."""

    def __init__(self, response: dict[str, object] | BaseException) -> None:
        self.response = response
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
            return {"synthetic_billing": True}
        assert path == ENDPOINT_POWER_PLANNER
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


async def fetch_latest(response: dict[str, object] | BaseException) -> KepcoBill:
    auth = PlannerAuth(response)
    with patch("custom_components.kepco_on.api.parse_bill", return_value=synthetic_bill()):
        return await KepcoOnClient(cast("Any", auth)).async_get_bill(customer())


@pytest.mark.parametrize("contract", CONTRACTS)
def test_apartment_contracts_preserve_identifiers_and_change_date(contract: str) -> None:
    parsed = customer(contract)
    original_key = customer(CONTRACTS[0]).stable_key
    assert parsed.is_supported is True
    assert parsed.is_house is False
    assert parsed.stable_key == original_key
    assert parsed.customer_number == "TEST_BUILDING"
    assert parsed.house_contract_number == "TEST_HOUSEHOLD"
    assert parsed.change_ymd == "20260409"
    assert deserialize_customer(serialize_customer(parsed)) == parsed


@pytest.mark.parametrize("field", ["CUST_NO", "SI_CUST_NO"])
def test_combined_contract_still_requires_both_identifiers(field: str) -> None:
    payload = customer_payload(CONTRACTS[1])
    rows = cast("list[dict[str, object]]", payload["dlt_myPageAppendList"])
    del rows[0][field]
    with pytest.raises(KepcoOnProtocolError, match="is missing"):
        parse_customers(payload, "TEST_ACCOUNT_HASH")


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", CONTRACTS)
async def test_latest_apartment_planner_uses_contract_specific_request(
    contract: str,
) -> None:
    auth = PlannerAuth(
        {"dma_powerPlanner": {"RETURN_CD": "00", "F_AP_QT": "123.45", "PREDICT_TOT": "99999"}}
    )
    base = synthetic_bill()
    with patch("custom_components.kepco_on.api.parse_bill", return_value=base):
        result = await KepcoOnClient(cast("Any", auth)).async_get_bill(customer(contract))
    assert auth.calls == [
        (
            ENDPOINT_APT_BILL_DETAIL,
            {
                "dma_search": {
                    "custNo": "TEST_BUILDING",
                    "housCntrNo": "TEST_HOUSEHOLD",
                    "yymm": "",
                    "yyyymm": "",
                    "searchType": "DETAIL",
                }
            },
            "mf_wfm_layout_sbm_search",
        ),
        (
            ENDPOINT_POWER_PLANNER,
            {
                "dma_search": {
                    "schYm": "",
                    "custNo": "TEST_BUILDING"
                    if contract == "아파트(종합계약)"
                    else "TEST_HOUSEHOLD",
                    "gubun": "",
                    "schChart": "12",
                    "CUST_NO": "",
                    "housCntrNo": "TEST_HOUSEHOLD" if contract == "아파트(종합계약)" else "",
                    "yyyymm": "",
                    "searchType": "",
                    "dong": "",
                    "ho": "",
                    "months": "",
                    "chgYmd": "" if contract == "아파트(종합계약)" else "20260409",
                }
            },
            "mf_wfm_layout_sbm_powerPlanner",
        ),
    ]
    expected_current = 0.12345 if contract == "아파트(종합계약)" else 123.45
    assert result.current_period_usage_kwh == pytest.approx(expected_current)
    if contract == "아파트(종합계약)":
        assert result.predicted_period_usage_kwh == pytest.approx(99.999)
    else:
        assert result.predicted_period_usage_kwh is None
    assert result.power_planner_status == "ok"
    assert result.power_planner_return_code == "00"
    assert result.usage_kwh == base.usage_kwh
    assert result.amount_krw == base.amount_krw
    assert result.history == base.history


@pytest.mark.asyncio
async def test_historical_apartment_bill_does_not_query_or_mix_current_period() -> None:
    auth = PlannerAuth(AssertionError("planner must not be queried"))
    base = synthetic_bill()
    with patch("custom_components.kepco_on.api.parse_bill", return_value=base):
        result = await KepcoOnClient(cast("Any", auth)).async_get_bill(customer(), "202607")
    assert result is base
    assert len(auth.calls) == 1
    assert result.power_planner_status == "not_requested"
    assert result.current_period_usage_kwh is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, "0", "0.000", 25.5, "1,234.56"])
async def test_valid_current_usage_including_zero_is_preserved(value: object) -> None:
    result = await fetch_latest({"dma_powerPlanner": {"RETURN_CD": "00", "F_AP_QT": value}})
    assert result.current_period_usage_kwh == pytest.approx(
        float(str(value).replace(",", "")) / 1000
    )
    assert result.power_planner_status == "ok"
    assert result.predicted_period_usage_kwh is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "null"])
async def test_missing_current_usage_is_not_fabricated(value: object) -> None:
    result = await fetch_latest(
        {"dma_powerPlanner": {"RETURN_CD": "00", "F_AP_QT": value, "PREDICT_TOT": "99999"}}
    )
    assert result.current_period_usage_kwh is None
    assert result.predicted_period_usage_kwh == pytest.approx(99.999)
    assert result.power_planner_current_status == "no_data"
    assert result.power_planner_prediction_status == "ok"
    assert result.power_planner_status == "ok"
    assert result.usage_kwh == synthetic_bill().usage_kwh


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["90", "01", "99"])
async def test_unsuccessful_return_code_never_publishes_numbers(code: str) -> None:
    result = await fetch_latest(
        {"dma_powerPlanner": {"RETURN_CD": code, "F_AP_QT": "123", "PREDICT_TOT": "99999"}}
    )
    assert result.current_period_usage_kwh is None
    assert result.predicted_period_usage_kwh is None
    assert result.power_planner_status == "no_data"
    assert result.power_planner_return_code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "-Infinity", "-1", "bad", [], 10**400])
async def test_invalid_optional_numbers_keep_billing_available(value: object) -> None:
    result = await fetch_latest({"dma_powerPlanner": {"RETURN_CD": "00", "F_AP_QT": value}})
    assert result.power_planner_status == "invalid_response"
    assert result.current_period_usage_kwh is None
    assert result.usage_kwh == synthetic_bill().usage_kwh
    assert result.amount_krw == synthetic_bill().amount_krw


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["PRIVATE_TOKEN_CANARY", "000", 0, True, [], {}])
async def test_invalid_status_does_not_expose_arbitrary_response_text(code: object) -> None:
    result = await fetch_latest({"dma_powerPlanner": {"RETURN_CD": code, "F_AP_QT": "123"}})
    assert result.power_planner_status == "invalid_response"
    assert result.power_planner_return_code is None
    assert "PRIVATE_TOKEN_CANARY" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"dma_powerPlanner": None}, {"dma_powerPlanner": {}}])
async def test_absent_planner_data_is_not_a_billing_error(response: dict[str, object]) -> None:
    result = await fetch_latest(response)
    assert result.power_planner_status == "no_data"
    assert result.usage_kwh == synthetic_bill().usage_kwh


@pytest.mark.asyncio
async def test_malformed_result_wrapper_is_distinguished_from_no_data() -> None:
    result = await fetch_latest({"dma_powerPlanner": []})
    assert result.power_planner_status == "invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status"),
    [
        (KepcoOnConnectionError("PRIVATE_TOKEN_CANARY"), "connection_error"),
        (KepcoOnRateLimitError("PRIVATE_TOKEN_CANARY"), "rate_limited"),
        (KepcoOnProtocolError("PRIVATE_TOKEN_CANARY"), "invalid_response"),
    ],
)
async def test_optional_failures_preserve_bills_and_safe_diagnostic_status(
    error: Exception, status: str
) -> None:
    result = await fetch_latest(error)
    assert result.power_planner_status == status
    assert result.amount_krw == synthetic_bill().amount_krw
    assert "PRIVATE_TOKEN_CANARY" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [KepcoOnAuthError("expired"), KepcoOnSessionExpired("expired"), asyncio.CancelledError()],
)
async def test_authentication_and_cancellation_are_not_swallowed(error: BaseException) -> None:
    with pytest.raises(type(error)):
        await fetch_latest(error)


def test_prediction_unit_is_not_inferred_from_sample_magnitude() -> None:
    for ambiguous in ("636.263", "99999", "garbled", None):
        assert parse_power_planner(
            {"dma_powerPlanner": {"RETURN_CD": "00", "F_AP_QT": "7.5", "PREDICT_TOT": ambiguous}}
        ) == (7.5, None)


@pytest.mark.asyncio
async def test_current_sensor_available_and_missing_prediction_explains_no_data() -> None:
    item = customer()
    current = replace(
        synthetic_bill(),
        current_period_usage_kwh=0.0,
        power_planner_status="ok",
        power_planner_return_code="00",
    )
    entities = await setup_entities(
        customers=(item,), bills_by_customer_key={item.stable_key: current}
    )
    sensors = by_key(entities)
    assert len(entities) == 34
    assert sensors["monthly_usage"].available is True
    assert sensors["current_period_usage"].available is True
    assert sensors["current_period_usage"].native_value == 0.0
    assert sensors["predicted_period_usage"].available is True
    assert sensors["predicted_period_usage"].native_value is None
    for key in ("current_period_usage", "predicted_period_usage"):
        attrs = sensors[key].extra_state_attributes
        assert sensors[key].unique_id == f"{item.stable_key}_{key}"
        assert attrs["data_source"] == "kepco_power_planner"
        assert attrs["return_code"] == "00"
        assert "billing_month" not in attrs
        assert "usage_period_start" not in attrs
        assert "usage_period_end" not in attrs
        encoded = json.dumps(attrs, ensure_ascii=False)
        assert "TEST_BUILDING" not in encoded
        assert "TEST_HOUSEHOLD" not in encoded
    assert sensors["current_period_usage"].extra_state_attributes["data_status"] == "ok"
    assert sensors["predicted_period_usage"].extra_state_attributes["data_status"] == "no_data"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["no_data", "not_requested", "connection_error", "rate_limited", "invalid_response"]
)
async def test_missing_planner_reason_does_not_disable_monthly_sensors(status: str) -> None:
    item = customer()
    current = replace(synthetic_bill(), power_planner_status=status)
    entities = await setup_entities(
        customers=(item,), bills_by_customer_key={item.stable_key: current}
    )
    sensors = by_key(entities)
    assert sensors["monthly_usage"].available is True
    for key in ("current_period_usage", "predicted_period_usage"):
        assert sensors[key].available is True
        assert sensors[key].extra_state_attributes["data_status"] == status
        assert sensors[key].extra_state_attributes["data_status_message"]
