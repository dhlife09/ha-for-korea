"""Korean electricity-charge display-name regression tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
DEVICE_NAME = "전기요금"
NO_BREAK_SPACE = chr(0xA0)
VISIBLE_NAMES = {
    "electricity_subtotal": "전기요금 계",
    "base_charge": "전기요금 상세 기본요금",
    "energy_charge": "전기요금 상세 전력량요금",
    "climate_environment_charge": "전기요금 상세 기후환경요금",
    "fuel_adjustment_charge": "전기요금 상세 연료비조정요금",
    "child_discount": "전기요금 상세 출산가구할인요금",
    "vat": "부가가치세",
    "power_industry_fund": "전력기금",
    "rounding_amount": "원단위절사금액",
    "amount_due": "청구금액",
}
PREFIXED_KEYS = frozenset(
    {
        "electricity_subtotal",
        "base_charge",
        "energy_charge",
        "climate_environment_charge",
        "fuel_adjustment_charge",
        "child_discount",
    }
)


def test_korean_charge_entity_names_remain_complete_on_device_page() -> None:
    translations = cast(
        "dict[str, Any]",
        json.loads(
            (ROOT / "custom_components" / "kepco_on" / "translations" / "ko.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    sensors = cast(
        "dict[str, dict[str, str]]",
        translations["entity"]["sensor"],
    )
    actual = {key: sensors[key]["name"] for key in VISIBLE_NAMES}
    visible_actual = {key: name.replace(NO_BREAK_SPACE, " ") for key, name in actual.items()}

    assert visible_actual == VISIBLE_NAMES
    protected_prefix = f"{DEVICE_NAME}{NO_BREAK_SPACE}"
    for key in PREFIXED_KEYS:
        assert actual[key].startswith(protected_prefix)
        assert not actual[key].startswith(f"{DEVICE_NAME} ")
