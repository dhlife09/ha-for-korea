"""Residential direct-contract API tests for KEPCO ON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from custom_components.kepco_on.api import KepcoOnClient
from custom_components.kepco_on.const import ENDPOINT_MAIN_CHART, ENDPOINT_POWER_PLANNER
from custom_components.kepco_on.exceptions import (
    KepcoOnConnectionError,
    KepcoOnProtocolError,
    KepcoOnRateLimitError,
)
from custom_components.kepco_on.models import KepcoCustomer

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads((FIXTURES / name).read_text(encoding="utf-8")),
    )


def _house_customer(*, change_ymd: str = "20260409") -> KepcoCustomer:
    return KepcoCustomer(
        stable_key="house-key",
        apartment_name="주택용/3kW",
        dong="",
        ho="",
        contract_method="주택용/3kW",
        is_supported=True,
        _customer_number="TEST_SI_CUST_001",
        _house_contract_number="TEST_SI_CUST_001",
        _change_ymd=change_ymd,
    )


@pytest.mark.asyncio
async def test_house_bill_combines_main_chart_and_power_planner() -> None:
    calls: list[tuple[str, dict[str, object] | None, str | None]] = []

    async def protected_request(
        path: str,
        payload: dict[str, object] | None,
        *,
        submission_id: str | None = None,
    ) -> dict[str, object]:
        calls.append((path, payload, submission_id))
        if path == ENDPOINT_MAIN_CHART:
            return _load_fixture("house_main_chart.json")
        assert path == ENDPOINT_POWER_PLANNER
        return _load_fixture("house_power_planner.json")

    class Auth:
        async_protected_request = staticmethod(protected_request)

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    bill = await KepcoOnClient(cast("Any", Auth())).async_get_bill(
        _house_customer(),
        "invalid-month-is-ignored-for-house-contracts",
    )

    assert bill.bill_month == "202608"
    assert bill.usage_kwh == 603
    assert bill.amount_krw == 142870
    assert bill.current_period_usage_kwh == pytest.approx(509.783)
    assert bill.predicted_period_usage_kwh is None
    assert calls == [
        (
            ENDPOINT_MAIN_CHART,
            {
                "dma_search": {
                    "schYm": "",
                    "custNo": "TEST_SI_CUST_001",
                    "gubun": "",
                    "schChart": "12",
                    "CUST_NO": "",
                    "housCntrNo": "",
                    "yyyymm": "",
                    "searchType": "",
                    "dong": "",
                    "ho": "",
                    "months": "13",
                    "chgYmd": "202604",
                }
            },
            "mf_wfm_layout_sbm_houseChart",
        ),
        (
            ENDPOINT_POWER_PLANNER,
            {
                "dma_search": {
                    "schYm": "",
                    "custNo": "TEST_SI_CUST_001",
                    "gubun": "",
                    "schChart": "12",
                    "CUST_NO": "",
                    "housCntrNo": "",
                    "yyyymm": "",
                    "searchType": "",
                    "dong": "",
                    "ho": "",
                    "months": "",
                    "chgYmd": "20260409",
                }
            },
            "mf_wfm_layout_sbm_powerPlanner",
        ),
    ]


@pytest.mark.asyncio
async def test_house_bill_keeps_history_when_power_planner_has_no_values() -> None:
    calls: list[tuple[str, dict[str, object] | None, str | None]] = []

    async def protected_request(
        path: str,
        payload: dict[str, object] | None,
        *,
        submission_id: str | None = None,
    ) -> dict[str, object]:
        calls.append((path, payload, submission_id))
        if path == ENDPOINT_MAIN_CHART:
            return _load_fixture("house_main_chart.json")
        assert path == ENDPOINT_POWER_PLANNER
        return {"rsMsg": {}}

    class Auth:
        async_protected_request = staticmethod(protected_request)

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    bill = await KepcoOnClient(cast("Any", Auth())).async_get_bill(_house_customer(change_ymd=""))

    assert bill.bill_month == "202608"
    assert bill.current_period_usage_kwh is None
    assert bill.predicted_period_usage_kwh is None
    assert len(calls) == 2
    main_payload = calls[0][1]
    assert isinstance(main_payload, dict)
    main_search = main_payload.get("dma_search")
    assert isinstance(main_search, dict)
    assert main_search["chgYmd"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [KepcoOnConnectionError, KepcoOnProtocolError, KepcoOnRateLimitError],
)
async def test_house_bill_keeps_history_when_power_planner_fails(
    error_type: type[Exception],
) -> None:
    calls: list[str] = []

    async def protected_request(
        path: str,
        payload: dict[str, object] | None,
        *,
        submission_id: str | None = None,
    ) -> dict[str, object]:
        del payload, submission_id
        calls.append(path)
        if path == ENDPOINT_MAIN_CHART:
            return _load_fixture("house_main_chart.json")
        assert path == ENDPOINT_POWER_PLANNER
        raise error_type("simulated optional Power Planner failure")

    class Auth:
        async_protected_request = staticmethod(protected_request)

        def account_uid_hash(self) -> str:
            return "ACCOUNT_HASH"

    bill = await KepcoOnClient(cast("Any", Auth())).async_get_bill(_house_customer())

    assert bill.bill_month == "202608"
    assert bill.usage_kwh == 603
    assert bill.current_period_usage_kwh is None
    assert bill.predicted_period_usage_kwh is None
    assert calls == [ENDPOINT_MAIN_CHART, ENDPOINT_POWER_PLANNER]
