"""Service action tests for KEPCO ON."""

from __future__ import annotations

import asyncio
import json
import traceback
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import voluptuous as vol
from custom_components.kepco_on.const import DOMAIN, OPT_HISTORY_MONTHS
from custom_components.kepco_on.exceptions import (
    KepcoOnConnectionError,
    KepcoOnProtocolError,
    KepcoOnSessionExpired,
)
from custom_components.kepco_on.models import (
    KepcoBill,
    KepcoChargeBreakdown,
    KepcoCoordinatorData,
    KepcoCustomer,
    KepcoUsageHistoryPoint,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.yaml import load_yaml_dict

ROOT = Path(__file__).resolve().parents[1]
RAW_CUSTOMER_SECRET = "RAW_CUST_1234567890"
RAW_HOUSE_SECRET = "RAW_HOUSE_0987654321"
TOKEN_SECRET = "TOKEN_SECRET_CANARY"
FULLWIDTH_MONTH = "\uff12\uff10\uff12\uff16\uff10\uff18"


@pytest.fixture(autouse=True)
def fixed_service_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin service month-window tests to a stable current month."""
    import custom_components.kepco_on.services as services

    monkeypatch.setattr(services, "_now", lambda: datetime(2026, 9, 1, tzinfo=UTC))


@dataclass(slots=True)
class RegisteredService:
    """Captured service registration."""

    handler: Callable[[ServiceCall], Awaitable[dict[str, Any]]]
    schema: Any
    supports_response: SupportsResponse


class FakeServices:
    """Small service registry surface used by setup tests."""

    def __init__(self) -> None:
        self.registered: dict[tuple[str, str], RegisteredService] = {}
        self.register_calls: list[tuple[str, str]] = []

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.registered

    def async_register(
        self,
        domain: str,
        service: str,
        service_func: Callable[[ServiceCall], Awaitable[dict[str, Any]]],
        schema: Any = None,
        *,
        supports_response: SupportsResponse = SupportsResponse.NONE,
        **_: Any,
    ) -> None:
        self.register_calls.append((domain, service))
        self.registered[(domain, service)] = RegisteredService(
            handler=service_func,
            schema=schema,
            supports_response=supports_response,
        )


class FakeConfigEntries:
    """Minimal config-entry lookup surface."""

    def __init__(self, entries: Mapping[str, FakeConfigEntry] | None = None) -> None:
        self.entries = dict(entries or {})

    def async_get_known_entry(self, entry_id: str) -> FakeConfigEntry | None:
        return self.entries.get(entry_id)


class FakeHass:
    """Small Home Assistant surface for service tests."""

    def __init__(self, entries: Mapping[str, FakeConfigEntry] | None = None) -> None:
        self.services = FakeServices()
        self.config_entries = FakeConfigEntries(entries)


class FakeSession:
    """Session state used by runtime validation tests."""

    def __init__(self, *, closed: bool = False) -> None:
        self.closed = closed


class FakeClient:
    """Client fake that records bill requests."""

    def __init__(self, results: list[KepcoBill | Exception]) -> None:
        self.results = results
        self.calls: list[tuple[KepcoCustomer, str | None]] = []

    async def async_get_bill(self, customer: KepcoCustomer, month: str | None = None) -> KepcoBill:
        self.calls.append((customer, month))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeConfigEntry:
    """Mutable config entry stand-in."""

    def __init__(
        self,
        *,
        entry_id: str = "entry-1",
        domain: str = DOMAIN,
        state: ConfigEntryState = ConfigEntryState.LOADED,
        runtime_data: Any = None,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        self.entry_id = entry_id
        self.domain = domain
        self.state = state
        self.runtime_data = runtime_data
        self.options = dict(options or {})


def customer(stable_key: str = "cust-key-1") -> KepcoCustomer:
    """Return a selected customer with raw identifiers as privacy canaries."""
    return KepcoCustomer(
        stable_key=stable_key,
        apartment_name="Secret Apartment",
        dong="101",
        ho="1001",
        contract_method="apartment",
        is_supported=True,
        _customer_number=f"{RAW_CUSTOMER_SECRET}_{stable_key}",
        _house_contract_number=f"{RAW_HOUSE_SECRET}_{stable_key}",
    )


def bill(
    month: str,
    *,
    usage: int | None = 321,
    amount: int | None = 96330,
    period_start: date | None = date(2026, 8, 1),
    period_end: date | None = date(2026, 8, 31),
    charge: KepcoChargeBreakdown | None = None,
    history: tuple[KepcoUsageHistoryPoint, ...] = (),
) -> KepcoBill:
    """Return a synthetic bill for response-shape tests."""
    return KepcoBill(
        bill_month=month,
        period_start=period_start,
        period_end=period_end,
        usage_kwh=usage,
        amount_krw=amount,
        charge=charge
        or KepcoChargeBreakdown(
            subtotal_krw=90000,
            base_krw=7300,
            energy_krw=65000,
            climate_krw=2800,
            fuel_krw=-1200,
            child_discount_krw=-8000,
            vat_krw=8200,
            fund_krw=2230,
            rounding_krw=-30,
        ),
        history=history,
    )


def runtime(
    customers: tuple[KepcoCustomer, ...],
    client: FakeClient,
    *,
    latest_bill: KepcoBill | None = None,
    session_closed: bool = False,
) -> SimpleNamespace:
    """Return runtime data with coordinator-selected customers."""
    data = KepcoCoordinatorData(
        customers=customers,
        bills_by_customer_key=({customers[0].stable_key: latest_bill} if latest_bill else {}),
    )
    return SimpleNamespace(
        client=client,
        coordinator=SimpleNamespace(data=data),
        session=FakeSession(closed=session_closed),
    )


async def setup_services(hass: FakeHass) -> None:
    """Register services through the integration setup path."""
    from custom_components.kepco_on import async_setup

    assert await async_setup(cast("Any", hass), {}) is True


async def call_action(
    registered: RegisteredService,
    hass: FakeHass,
    service: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the registered schema and invoke a captured action handler."""
    validated = registered.schema(dict(data))
    response = await registered.handler(
        ServiceCall(cast("Any", hass), DOMAIN, service, cast("dict[str, Any]", validated))
    )
    json.dumps(response)
    return response


def assert_safe_exception_and_response(
    err: BaseException,
    response: Mapping[str, Any] | None = None,
) -> None:
    """Assert service failures do not retain or render sensitive data."""
    assert err.__cause__ is None
    assert err.__context__ is None
    rendered = "\n".join(
        (
            str(err),
            repr(err),
            "".join(traceback.format_exception(err)),
            json.dumps(response or {}),
        )
    )
    assert TOKEN_SECRET not in rendered
    assert RAW_CUSTOMER_SECRET not in rendered
    assert RAW_HOUSE_SECRET not in rendered


@pytest.mark.asyncio
async def test_async_setup_registers_response_actions_without_entries_idempotently() -> None:
    hass = FakeHass()

    await setup_services(hass)
    await setup_services(hass)

    assert hass.services.register_calls == [
        (DOMAIN, "get_monthly_bill"),
        (DOMAIN, "get_usage_history"),
    ]
    for service in ("get_monthly_bill", "get_usage_history"):
        registered = hass.services.registered[(DOMAIN, service)]
        assert registered.supports_response is SupportsResponse.ONLY


@pytest.mark.asyncio
async def test_monthly_bill_schema_rejects_missing_or_invalid_month() -> None:
    selected = customer()
    entry = FakeConfigEntry(runtime_data=runtime((selected,), FakeClient([])))
    hass = FakeHass({entry.entry_id: entry})
    await setup_services(hass)
    registered = hass.services.registered[(DOMAIN, "get_monthly_bill")]

    with pytest.raises(vol.Invalid):
        registered.schema({"config_entry_id": entry.entry_id, "customer_id": selected.stable_key})

    for month in ("", "   ", "2026-08", FULLWIDTH_MONTH, "202613", "202610", "202408"):
        with pytest.raises(ServiceValidationError) as raised:
            await call_action(
                registered,
                hass,
                "get_monthly_bill",
                {
                    "config_entry_id": entry.entry_id,
                    "customer_id": selected.stable_key,
                    "month": month,
                },
            )
        assert raised.value.translation_domain == DOMAIN
        assert raised.value.translation_key == "invalid_month"


@pytest.mark.asyncio
async def test_usage_history_normalizes_blank_month_to_latest_bill() -> None:
    selected = customer()
    latest = bill(
        "202608",
        history=(KepcoUsageHistoryPoint("202607", 120), KepcoUsageHistoryPoint("202608", 130)),
    )
    entry = FakeConfigEntry(
        runtime_data=runtime((selected,), FakeClient([]), latest_bill=latest),
    )
    hass = FakeHass({entry.entry_id: entry})
    await setup_services(hass)
    registered = hass.services.registered[(DOMAIN, "get_usage_history")]

    for raw_month in (None, "", "   "):
        response = await call_action(
            registered,
            hass,
            "get_usage_history",
            {
                "config_entry_id": entry.entry_id,
                "customer_id": selected.stable_key,
                "month": raw_month,
            },
        )
        assert response == {
            "history": [
                {"month": "202607", "usage_kwh": 120},
                {"month": "202608", "usage_kwh": 130},
            ]
        }


@pytest.mark.asyncio
async def test_usage_history_schema_rejects_invalid_months() -> None:
    selected = customer()
    entry = FakeConfigEntry(runtime_data=runtime((selected,), FakeClient([])))
    hass = FakeHass({entry.entry_id: entry})
    await setup_services(hass)
    registered = hass.services.registered[(DOMAIN, "get_usage_history")]

    for value in ("2026-08", FULLWIDTH_MONTH, "202600", "202610", "202408"):
        with pytest.raises(ServiceValidationError) as raised:
            await call_action(
                registered,
                hass,
                "get_usage_history",
                {
                    "config_entry_id": entry.entry_id,
                    "customer_id": selected.stable_key,
                    "month": value,
                },
            )
        assert raised.value.translation_domain == DOMAIN
        assert raised.value.translation_key == "invalid_month"


@pytest.mark.asyncio
async def test_service_month_window_uses_home_assistant_local_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on.services as services

    monkeypatch.setattr(
        services,
        "_now",
        lambda: datetime(2026, 9, 1, 0, 30, tzinfo=timezone(timedelta(hours=9))),
    )
    selected = customer("selected-key")
    client = FakeClient([bill("202609")])
    entry = FakeConfigEntry(runtime_data=runtime((selected,), client))
    hass = FakeHass({entry.entry_id: entry})
    await setup_services(hass)

    response = await call_action(
        hass.services.registered[(DOMAIN, "get_monthly_bill")],
        hass,
        "get_monthly_bill",
        {"config_entry_id": entry.entry_id, "customer_id": selected.stable_key, "month": "202609"},
    )

    assert client.calls == [(selected, "202609")]
    assert response["billing_month"] == "202609"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "translation_key"),
    [
        (None, "invalid_entry"),
        (FakeConfigEntry(domain="other"), "invalid_entry"),
        (FakeConfigEntry(state=ConfigEntryState.NOT_LOADED), "invalid_entry"),
        (FakeConfigEntry(runtime_data=None), "invalid_entry"),
        (
            FakeConfigEntry(
                runtime_data=SimpleNamespace(
                    client=FakeClient([]),
                    coordinator=SimpleNamespace(data=None),
                    session=FakeSession(),
                )
            ),
            "invalid_entry",
        ),
        (
            FakeConfigEntry(
                runtime_data=runtime((customer(),), FakeClient([]), session_closed=True)
            ),
            "invalid_entry",
        ),
    ],
)
async def test_entry_validation_uses_translated_safe_errors(
    entry: FakeConfigEntry | None,
    translation_key: str,
) -> None:
    entries = {} if entry is None else {entry.entry_id: entry}
    hass = FakeHass(entries)
    await setup_services(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await call_action(
            hass.services.registered[(DOMAIN, "get_monthly_bill")],
            hass,
            "get_monthly_bill",
            {"config_entry_id": "entry-1", "customer_id": "cust-key-1", "month": "202608"},
        )

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == translation_key
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_customer_validation_allows_only_selected_stable_keys() -> None:
    selected = customer("selected-key")
    entry = FakeConfigEntry(runtime_data=runtime((selected,), FakeClient([])))
    hass = FakeHass({entry.entry_id: entry})
    await setup_services(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await call_action(
            hass.services.registered[(DOMAIN, "get_monthly_bill")],
            hass,
            "get_monthly_bill",
            {
                "config_entry_id": entry.entry_id,
                "customer_id": RAW_CUSTOMER_SECRET,
                "month": "202608",
            },
        )

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "invalid_customer"
    assert_safe_exception_and_response(raised.value)


@pytest.mark.asyncio
async def test_get_monthly_bill_returns_exact_safe_shape() -> None:
    selected = customer("selected-key")
    client = FakeClient([bill("202608")])
    entry = FakeConfigEntry(runtime_data=runtime((selected,), client))
    hass = FakeHass({entry.entry_id: entry})
    await setup_services(hass)

    response = await call_action(
        hass.services.registered[(DOMAIN, "get_monthly_bill")],
        hass,
        "get_monthly_bill",
        {"config_entry_id": entry.entry_id, "customer_id": selected.stable_key, "month": "202608"},
    )

    assert client.calls == [(selected, "202608")]
    assert response == {
        "billing_month": "202608",
        "usage_period_start": "2026-08-01",
        "usage_period_end": "2026-08-31",
        "usage_kwh": 321,
        "amount_due_krw": 96330,
        "charge_breakdown": {
            "subtotal_krw": 90000,
            "base_krw": 7300,
            "energy_krw": 65000,
            "climate_krw": 2800,
            "fuel_krw": -1200,
            "child_discount_krw": -8000,
            "vat_krw": 8200,
            "fund_krw": 2230,
            "rounding_krw": -30,
        },
    }
    encoded = json.dumps(response)
    assert RAW_CUSTOMER_SECRET not in encoded
    assert RAW_HOUSE_SECRET not in encoded
    assert "Secret Apartment" not in encoded


@pytest.mark.asyncio
async def test_get_monthly_bill_omits_null_charge_fields_and_keeps_safe_null_dates() -> None:
    selected = customer("selected-key")
    client = FakeClient(
        [
            bill(
                "202608",
                period_start=None,
                period_end=None,
                charge=KepcoChargeBreakdown(base_krw=7300, vat_krw=None),
            )
        ]
    )
    entry = FakeConfigEntry(runtime_data=runtime((selected,), client))
    hass = FakeHass({entry.entry_id: entry})
    await setup_services(hass)

    response = await call_action(
        hass.services.registered[(DOMAIN, "get_monthly_bill")],
        hass,
        "get_monthly_bill",
        {"config_entry_id": entry.entry_id, "customer_id": selected.stable_key, "month": "202608"},
    )

    assert response["usage_period_start"] is None
    assert response["usage_period_end"] is None
    assert response["charge_breakdown"] == {"base_krw": 7300}


@pytest.mark.asyncio
async def test_get_usage_history_uses_latest_bill_when_month_omitted_and_slices_ascending() -> None:
    selected = customer("selected-key")
    latest = bill(
        "202608",
        history=(
            KepcoUsageHistoryPoint("202606", 100),
            KepcoUsageHistoryPoint("202605", 90),
            KepcoUsageHistoryPoint("202608", 130),
            KepcoUsageHistoryPoint("202607", 120),
        ),
    )
    client = FakeClient([])
    entry = FakeConfigEntry(
        runtime_data=runtime((selected,), client, latest_bill=latest),
        options={OPT_HISTORY_MONTHS: 3},
    )
    hass = FakeHass({entry.entry_id: entry})
    await setup_services(hass)

    response = await call_action(
        hass.services.registered[(DOMAIN, "get_usage_history")],
        hass,
        "get_usage_history",
        {"config_entry_id": entry.entry_id, "customer_id": selected.stable_key},
    )

    assert client.calls == []
    assert response == {
        "history": [
            {"month": "202606", "usage_kwh": 100},
            {"month": "202607", "usage_kwh": 120},
            {"month": "202608", "usage_kwh": 130},
        ]
    }


@pytest.mark.asyncio
async def test_get_usage_history_fetches_when_month_supplied_or_latest_missing() -> None:
    selected = customer("selected-key")
    fetched = bill(
        "202607",
        history=(KepcoUsageHistoryPoint("202607", 120), KepcoUsageHistoryPoint("202606", 100)),
    )
    client = FakeClient([fetched])
    entry = FakeConfigEntry(runtime_data=runtime((selected,), client))
    hass = FakeHass({entry.entry_id: entry})
    await setup_services(hass)

    response = await call_action(
        hass.services.registered[(DOMAIN, "get_usage_history")],
        hass,
        "get_usage_history",
        {"config_entry_id": entry.entry_id, "customer_id": selected.stable_key, "month": "202607"},
    )

    assert client.calls == [(selected, "202607")]
    assert response == {
        "history": [
            {"month": "202606", "usage_kwh": 100},
            {"month": "202607", "usage_kwh": 120},
        ]
    }


@pytest.mark.asyncio
async def test_concurrent_service_calls_return_independent_safe_responses() -> None:
    customer_a = customer("key-a")
    customer_b = customer("key-b")
    client_a = FakeClient([bill("202608", usage=111)])
    client_b = FakeClient([bill("202608", usage=222)])
    entry_a = FakeConfigEntry(entry_id="entry-a", runtime_data=runtime((customer_a,), client_a))
    entry_b = FakeConfigEntry(entry_id="entry-b", runtime_data=runtime((customer_b,), client_b))
    hass = FakeHass({entry_a.entry_id: entry_a, entry_b.entry_id: entry_b})
    await setup_services(hass)
    registered = hass.services.registered[(DOMAIN, "get_monthly_bill")]

    response_a, response_b = await asyncio.gather(
        call_action(
            registered,
            hass,
            "get_monthly_bill",
            {
                "config_entry_id": entry_a.entry_id,
                "customer_id": customer_a.stable_key,
                "month": "202608",
            },
        ),
        call_action(
            registered,
            hass,
            "get_monthly_bill",
            {
                "config_entry_id": entry_b.entry_id,
                "customer_id": customer_b.stable_key,
                "month": "202608",
            },
        ),
    )

    assert response_a["usage_kwh"] == 111
    assert response_b["usage_kwh"] == 222
    assert client_a.calls == [(customer_a, "202608")]
    assert client_b.calls == [(customer_b, "202608")]


@pytest.mark.parametrize(
    ("service", "payload", "client_results"),
    [
        (
            "get_monthly_bill",
            {"month": "202608"},
            [KepcoOnProtocolError(f"bad {TOKEN_SECRET} {RAW_CUSTOMER_SECRET}")],
        ),
        (
            "get_usage_history",
            {"month": "202608"},
            [KepcoOnConnectionError(f"down {TOKEN_SECRET} {RAW_CUSTOMER_SECRET}")],
        ),
    ],
)
@pytest.mark.asyncio
async def test_api_errors_raise_translated_safe_failure_without_secret_leakage(
    service: str,
    payload: Mapping[str, Any],
    client_results: list[KepcoBill | Exception],
) -> None:
    selected = customer("selected-key")
    client = FakeClient(client_results)
    entry = FakeConfigEntry(runtime_data=runtime((selected,), client))
    hass = FakeHass({entry.entry_id: entry})
    await setup_services(hass)
    response: dict[str, Any] | None = None

    with pytest.raises(HomeAssistantError) as raised:
        response = await call_action(
            hass.services.registered[(DOMAIN, service)],
            hass,
            service,
            {
                "config_entry_id": entry.entry_id,
                "customer_id": selected.stable_key,
                **payload,
            },
        )

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "service_failed"
    assert_safe_exception_and_response(raised.value, response)


@pytest.mark.asyncio
async def test_auth_expiry_is_reported_as_safe_service_failure() -> None:
    selected = customer("selected-key")
    client = FakeClient([KepcoOnSessionExpired(f"expired {TOKEN_SECRET}")])
    entry = FakeConfigEntry(runtime_data=runtime((selected,), client))
    hass = FakeHass({entry.entry_id: entry})
    await setup_services(hass)

    with pytest.raises(HomeAssistantError) as raised:
        await call_action(
            hass.services.registered[(DOMAIN, "get_monthly_bill")],
            hass,
            "get_monthly_bill",
            {
                "config_entry_id": entry.entry_id,
                "customer_id": selected.stable_key,
                "month": "202608",
            },
        )

    assert raised.value.translation_key == "service_failed"
    assert_safe_exception_and_response(raised.value)


def test_services_yaml_and_translations_have_action_metadata_parity() -> None:
    services = load_yaml_dict(ROOT / "custom_components/kepco_on/services.yaml")
    strings = json.loads(
        (ROOT / "custom_components/kepco_on/strings.json").read_text(encoding="utf-8")
    )
    en = json.loads(
        (ROOT / "custom_components/kepco_on/translations/en.json").read_text(encoding="utf-8")
    )
    ko = json.loads(
        (ROOT / "custom_components/kepco_on/translations/ko.json").read_text(encoding="utf-8")
    )

    assert set(services) == {"get_monthly_bill", "get_usage_history"}
    assert set(strings["services"]) == set(services)
    assert set(en["services"]) == set(services)
    assert set(ko["services"]) == set(services)
    assert strings["exceptions"]["invalid_entry"]["message"]
    assert set(strings["exceptions"]) == set(en["exceptions"]) == set(ko["exceptions"])

    customer_selector = services["get_monthly_bill"]["fields"]["customer_id"]["selector"]
    assert customer_selector == {"text": {}}
    assert (
        "raw KEPCO customer number"
        in services["get_monthly_bill"]["fields"]["customer_id"]["description"]
    )
    assert services["get_monthly_bill"]["fields"]["config_entry_id"]["selector"] == {
        "config_entry": {"integration": DOMAIN}
    }


@pytest.mark.asyncio
async def test_get_usage_history_includes_billed_amount_only_when_known() -> None:
    selected = customer("selected-key")
    latest = bill(
        "202608",
        history=(
            KepcoUsageHistoryPoint("202606", 417, 88450),
            KepcoUsageHistoryPoint("202607", 457, 101230),
            KepcoUsageHistoryPoint("202608", 603),
        ),
    )
    entry = FakeConfigEntry(
        runtime_data=runtime((selected,), FakeClient([]), latest_bill=latest),
    )
    hass = FakeHass({entry.entry_id: entry})
    await setup_services(hass)

    response = await call_action(
        hass.services.registered[(DOMAIN, "get_usage_history")],
        hass,
        "get_usage_history",
        {"config_entry_id": entry.entry_id, "customer_id": selected.stable_key},
    )

    assert response == {
        "history": [
            {"month": "202606", "usage_kwh": 417, "amount_krw": 88450},
            {"month": "202607", "usage_kwh": 457, "amount_krw": 101230},
            {"month": "202608", "usage_kwh": 603},
        ]
    }
