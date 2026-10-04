"""Residential customer model and persistence boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from custom_components.kepco_on.const import CONF_CUSTOMERS, CONF_SELECTED_CUSTOMERS
from custom_components.kepco_on.models import (
    KepcoAccountSession,
    KepcoCookie,
    KepcoCoordinatorData,
    KepcoCustomer,
    customer_location_name,
    selected_customer_location_title,
    serialize_customer,
    strict_selected_stored_customers,
)


def _customer(
    *,
    contract_method: str = "단일계약",
    apartment_name: str = "테스트 아파트",
    dong: str = "0101",
    ho: str = "0202",
) -> KepcoCustomer:
    return KepcoCustomer(
        stable_key="customer-key",
        apartment_name=apartment_name,
        dong=dong,
        ho=ho,
        contract_method=contract_method,
        is_supported=True,
        _customer_number="CUSTOMER_NUMBER",
        _house_contract_number="HOUSE_CONTRACT_NUMBER",
    )


def test_naive_session_and_coordinator_timestamps_are_normalized_to_utc() -> None:
    naive_timestamp = datetime(2026, 9, 3, 12, 34, 56)
    cookie = KepcoCookie(name="session", value="secret")

    session = KepcoAccountSession(
        refresh_token="refresh",
        user_id="user",
        member_name="member",
        updated_at=naive_timestamp,
        cookies=(cookie,),
    )
    coordinator_data = KepcoCoordinatorData(
        customers=(_customer(),),
        last_success=naive_timestamp,
    )

    expected = naive_timestamp.replace(tzinfo=UTC)
    assert session.updated_at == expected
    assert coordinator_data.last_success == expected


def test_customer_location_name_supports_house_and_text_location_values() -> None:
    house = _customer(
        contract_method="주택용/3kW",
        apartment_name="주택용/3kW",
        dong="-",
        ho="-",
    )
    text_location = _customer(dong=" A동 ", ho=" B호 ")

    assert customer_location_name(house) == "주택용/3kW"
    assert customer_location_name(text_location) == "A동동 B호호"


def test_selected_customer_location_title_rejects_empty_selection() -> None:
    with pytest.raises(ValueError, match="customers are unavailable"):
        selected_customer_location_title(())


def test_strict_stored_customers_rejects_missing_customer_data() -> None:
    with pytest.raises(ValueError, match="customers are unavailable"):
        strict_selected_stored_customers({})


def test_strict_stored_customers_rejects_unknown_selection() -> None:
    customer = _customer()
    entry_data = {
        CONF_CUSTOMERS: [serialize_customer(customer)],
        CONF_SELECTED_CUSTOMERS: ["unknown-key"],
    }

    with pytest.raises(ValueError, match="selection is invalid"):
        strict_selected_stored_customers(entry_data)
