"""Diagnostics privacy tests for KEPCO ON."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from custom_components.kepco_on.const import (
    CONF_CUSTOMERS,
    CONF_SELECTED_CUSTOMERS,
    CONF_SESSION_HANDOFF,
    CONF_USERNAME,
    OPT_POLLING_INTERVAL_HOURS,
)
from custom_components.kepco_on.models import KepcoBill, KepcoCoordinatorData, KepcoCustomer
from homeassistant.const import CONF_PASSWORD

PASSWORD_SECRET = "PASSWORD_SECRET_CANARY"
TOKEN_SECRET = "TOKEN_SECRET_CANARY"
USER_ID_SECRET = "USER_ID_SECRET_CANARY"
MEMBER_SECRET = "MEMBER_SECRET_CANARY"
COOKIE_SECRET = "COOKIE_SECRET_CANARY"
CUSTOMER_SECRET = "CUSTOMER_SECRET_CANARY"
CONTRACT_SECRET = "CONTRACT_SECRET_CANARY"
ADDRESS_SECRET = "ADDRESS_SECRET_CANARY"
PHONE_SECRET = "PHONE_SECRET_CANARY"
EMAIL_SECRET = "EMAIL_SECRET_CANARY"
RAW_BODY_SECRET = "RAW_BODY_SECRET_CANARY"
HISTORY_SECRET = "HISTORY_SECRET_CANARY"
JWT_SECRET = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret.payload"
LONG_CUSTOMER_ID_SECRET = "12345678901234567890"
VALID_CUSTOMER_HASH = "a" * 64
NUMERIC_VALID_CUSTOMER_HASH = "1" * 64
SECOND_VALID_CUSTOMER_HASH = "b" * 64
UPPERCASE_CUSTOMER_HASH = "C" * 64
SHORT_CUSTOMER_HASH = "d" * 63


class FakeHass:
    """Minimal hass object exposing Home Assistant version."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(version="2026.8.0")


class FakeConfigEntry:
    """Mutable config entry stand-in for diagnostics."""

    def __init__(self, runtime_data: Any = None) -> None:
        self.entry_id = "entry-1"
        self.title = "TITLE_SECRET_CANARY"
        self.data = {
            CONF_USERNAME: "USERNAME_SECRET_CANARY",
            CONF_PASSWORD: PASSWORD_SECRET,
            CONF_SESSION_HANDOFF: {
                "token": TOKEN_SECRET,
                "cookies": [{"name": "JSESSIONID", "value": COOKIE_SECRET}],
                "userId": USER_ID_SECRET,
                "memberName": MEMBER_SECRET,
            },
            CONF_CUSTOMERS: [
                {
                    "stable_key": "selected-key",
                    "customer_number": CUSTOMER_SECRET,
                    "house_contract_number": CONTRACT_SECRET,
                    "address": ADDRESS_SECRET,
                    "phone": PHONE_SECRET,
                    "email": EMAIL_SECRET,
                    "apartment_name": "APT_SECRET_CANARY",
                    "dong": "DONG_SECRET_CANARY",
                    "ho": "HO_SECRET_CANARY",
                }
            ],
            CONF_SELECTED_CUSTOMERS: ["selected-key"],
            "raw_api_body": RAW_BODY_SECRET,
        }
        self.options = {
            OPT_POLLING_INTERVAL_HOURS: 12,
            "nestedCookie": COOKIE_SECRET,
            "history": [{"charge": HISTORY_SECRET}],
        }
        self.runtime_data = runtime_data


def customer(stable_key: str = "selected-key") -> KepcoCustomer:
    """Return a customer with raw IDs as canaries."""
    return KepcoCustomer(
        stable_key=stable_key,
        apartment_name="APT_SECRET_CANARY",
        dong="DONG_SECRET_CANARY",
        ho="HO_SECRET_CANARY",
        contract_method="apartment",
        is_supported=True,
        _customer_number=CUSTOMER_SECRET,
        _house_contract_number=CONTRACT_SECRET,
    )


@pytest.mark.asyncio
async def test_diagnostics_exposes_only_safe_selected_customer_hashes() -> None:
    """Diagnostics exposes response-action customer hashes without raw identifiers."""
    from custom_components.kepco_on.diagnostics import async_get_config_entry_diagnostics

    coordinator = SimpleNamespace(
        last_update_success=True,
        data=KepcoCoordinatorData(
            customers=(
                customer(VALID_CUSTOMER_HASH),
                customer(NUMERIC_VALID_CUSTOMER_HASH),
                customer(UPPERCASE_CUSTOMER_HASH),
                customer(SHORT_CUSTOMER_HASH),
                customer(LONG_CUSTOMER_ID_SECRET),
                customer(CUSTOMER_SECRET),
                customer(SECOND_VALID_CUSTOMER_HASH),
            ),
        ),
    )
    entry = FakeConfigEntry(SimpleNamespace(coordinator=coordinator))

    diagnostics = await async_get_config_entry_diagnostics(cast("Any", FakeHass()), entry)
    encoded = json.dumps(diagnostics, ensure_ascii=False, default=str)

    assert diagnostics["config_entry"]["selected_customer_ids"] == [
        VALID_CUSTOMER_HASH,
        NUMERIC_VALID_CUSTOMER_HASH,
        SECOND_VALID_CUSTOMER_HASH,
    ]
    assert UPPERCASE_CUSTOMER_HASH not in encoded
    assert SHORT_CUSTOMER_HASH not in encoded
    assert LONG_CUSTOMER_ID_SECRET not in encoded
    assert CUSTOMER_SECRET not in encoded
    assert "APT_SECRET_CANARY" not in encoded
    assert "DONG_SECRET_CANARY" not in encoded
    assert "HO_SECRET_CANARY" not in encoded


@pytest.mark.asyncio
async def test_diagnostics_returns_whitelisted_summary_without_private_canaries() -> None:
    """Diagnostics exposes counts and field names, never raw config/runtime values."""
    from custom_components.kepco_on.diagnostics import async_get_config_entry_diagnostics

    bill = KepcoBill(
        bill_month="202608",
        usage_kwh=321,
        amount_krw=96330,
        history=(),
    )
    coordinator = SimpleNamespace(
        last_update_success=True,
        data=KepcoCoordinatorData(
            customers=(customer(),),
            bills_by_customer_key={"selected-key": bill},
            errors_by_customer_key=cast(
                "Any",
                {
                    "selected-key": "protocol_error",
                    "other-key": f"api_error:{TOKEN_SECRET}",
                    "jwt-key": JWT_SECRET,
                    "email-key": EMAIL_SECRET,
                    "phone-key": PHONE_SECRET,
                    "long-id-key": LONG_CUSTOMER_ID_SECRET,
                    "nested-key": {
                        "token": TOKEN_SECRET,
                        "error": ["protocol_error", JWT_SECRET, EMAIL_SECRET],
                    },
                    "none-key": None,
                },
            ),
            last_success=datetime(2026, 9, 1, 1, 2, 3, tzinfo=UTC),
        ),
    )
    runtime = SimpleNamespace(
        coordinator=coordinator,
        session=SimpleNamespace(
            closed=False,
            cookie_jar=[{"name": "JSESSIONID", "value": COOKIE_SECRET}],
            token=TOKEN_SECRET,
        ),
        raw_body=RAW_BODY_SECRET,
    )
    entry = FakeConfigEntry(runtime)

    diagnostics = await async_get_config_entry_diagnostics(cast("Any", FakeHass()), entry)
    encoded = json.dumps(diagnostics, ensure_ascii=False, default=str)

    assert diagnostics == {
        "integration": {
            "version": "1.3.0",
            "home_assistant_version": "2026.8.0",
        },
        "config_entry": {
            "account_type": "INDI",
            "polling_interval_hours": 12,
            "selected_customer_count": 1,
            "selected_customer_ids": [],
        },
        "runtime": {
            "loaded": True,
            "last_update_success": True,
            "last_success": "2026-09-01T01:02:03+00:00",
        },
        "availability": {
            "customers": 1,
            "bills": 1,
            "bill_errors": 8,
        },
        "parsed_fields": {
            "bill": [
                "amount_krw",
                "apartment_average_kwh",
                "bill_month",
                "building_average_kwh",
                "charge",
                "current_meter_reading",
                "history",
                "household_usage_kwh",
                "common_usage_kwh",
                "last_year_usage_kwh",
                "meter_reading_day",
                "period_end",
                "period_start",
                "previous_meter_reading",
                "previous_usage_kwh",
                "response_bill_month",
                "usage_kwh",
            ],
            "charge": [
                "base_krw",
                "child_discount_krw",
                "climate_krw",
                "energy_krw",
                "fuel_krw",
                "fund_krw",
                "rounding_krw",
                "subtotal_krw",
                "vat_krw",
            ],
            "history": ["amount_krw", "month", "usage_kwh"],
        },
        "error_categories": {
            "protocol_error": 1,
            "unknown_error": 7,
        },
    }
    for secret in (
        PASSWORD_SECRET,
        TOKEN_SECRET,
        USER_ID_SECRET,
        MEMBER_SECRET,
        COOKIE_SECRET,
        CUSTOMER_SECRET,
        CONTRACT_SECRET,
        ADDRESS_SECRET,
        PHONE_SECRET,
        EMAIL_SECRET,
        RAW_BODY_SECRET,
        HISTORY_SECRET,
        JWT_SECRET,
        LONG_CUSTOMER_ID_SECRET,
        "TITLE_SECRET_CANARY",
        "USERNAME_SECRET_CANARY",
        "APT_SECRET_CANARY",
        "DONG_SECRET_CANARY",
        "HO_SECRET_CANARY",
        "321",
        "96330",
        "202608",
    ):
        assert secret not in encoded


@pytest.mark.asyncio
async def test_diagnostics_handles_missing_or_malformed_runtime_safely() -> None:
    """Diagnostics must not raise or leak when runtime is absent or malformed."""
    from custom_components.kepco_on.diagnostics import async_get_config_entry_diagnostics

    entry = FakeConfigEntry(runtime_data=SimpleNamespace(coordinator=object(), token=TOKEN_SECRET))

    diagnostics = await async_get_config_entry_diagnostics(cast("Any", FakeHass()), entry)
    encoded = json.dumps(diagnostics, ensure_ascii=False, default=str)

    assert diagnostics["runtime"]["loaded"] is True
    assert diagnostics["runtime"]["last_update_success"] is None
    assert diagnostics["config_entry"]["selected_customer_ids"] == []
    assert diagnostics["availability"] == {"customers": 0, "bills": 0, "bill_errors": 0}
    assert TOKEN_SECRET not in encoded
    assert PASSWORD_SECRET not in encoded


@pytest.mark.asyncio
async def test_diagnostics_falls_back_for_malformed_options_and_non_sized_values() -> None:
    from custom_components.kepco_on.diagnostics import async_get_config_entry_diagnostics

    entry = FakeConfigEntry(runtime_data=None)
    cast("dict[str, Any]", entry.data)[CONF_SELECTED_CUSTOMERS] = object()
    entry.options = cast("dict[str, Any]", object())

    diagnostics = await async_get_config_entry_diagnostics(cast("Any", FakeHass()), entry)

    assert diagnostics["config_entry"]["polling_interval_hours"] == 12
    assert diagnostics["config_entry"]["selected_customer_count"] == 0
    assert diagnostics["runtime"]["loaded"] is False


@pytest.mark.asyncio
async def test_diagnostics_handles_bad_polling_option_and_dataclass_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on.diagnostics as diagnostics_module
    from custom_components.kepco_on.diagnostics import async_get_config_entry_diagnostics

    monkeypatch.setattr(diagnostics_module, "KepcoBill", object)
    entry = FakeConfigEntry(runtime_data=None)
    entry.options[OPT_POLLING_INTERVAL_HOURS] = object()

    diagnostics = await async_get_config_entry_diagnostics(cast("Any", FakeHass()), entry)

    assert diagnostics["config_entry"]["polling_interval_hours"] == 12
    assert diagnostics["parsed_fields"]["bill"] == []


@pytest.mark.asyncio
async def test_diagnostics_returns_empty_mapping_if_sanitizer_shape_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.kepco_on.diagnostics as diagnostics_module
    from custom_components.kepco_on.diagnostics import async_get_config_entry_diagnostics

    monkeypatch.setattr(diagnostics_module, "_sanitize", lambda value, **kwargs: object())

    assert (
        await async_get_config_entry_diagnostics(cast("Any", FakeHass()), FakeConfigEntry()) == {}
    )
