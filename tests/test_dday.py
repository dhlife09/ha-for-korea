"""Local date countdown, yearly recurrence and real HA flow/entity tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.kepco_on.const import DOMAIN
from custom_components.kepco_on.dday import DdayOptionsFlow, occurrence, validate_settings
from pytest_homeassistant_custom_component.common import MockConfigEntry

DATA: dict[str, Any] = {
    "service": "dday",
    "event_name": "테스트 일정",
    "target_date": "2026-01-05",
    "yearly": False,
}


@pytest.mark.parametrize(
    ("target", "today", "yearly", "expected"),
    [
        ("2026-01-05", "2026-01-04", False, "2026-01-05"),
        ("2026-01-05", "2026-01-05", True, "2026-01-05"),
        ("2026-01-05", "2026-01-06", False, "2026-01-05"),
        ("2026-01-05", "2026-01-06", True, "2027-01-05"),
        ("2024-02-29", "2025-02-27", True, "2025-02-28"),
        ("2024-02-29", "2025-03-01", True, "2026-02-28"),
        ("2024-02-29", "2028-02-01", True, "2028-02-29"),
        ("2028-02-29", "2026-02-01", True, "2028-02-29"),
    ],
)
def test_recurrence(target: str, today: str, yearly: bool, expected: str) -> None:
    assert (
        occurrence(
            {**DATA, "target_date": target, "yearly": yearly}, date.fromisoformat(today)
        ).isoformat()
        == expected
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"event_name": ""},
        {"event_name": "a" * 61},
        {"event_name": "line\nname"},
        {"target_date": "2025-02-29"},
        {"target_date": "9999-01-01"},
        {"yearly": "True"},
    ],
)
def test_invalid_settings(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"Invalid|day .* range"):
        validate_settings({**DATA, **changes})


async def test_setup_midnight_options_and_unload(
    hass: Any, enable_custom_integrations: None
) -> None:
    from custom_components.kepco_on.config_flow import KepcoOnConfigFlow
    from custom_components.kepco_on.diagnostics import async_get_config_entry_diagnostics
    from homeassistant.helpers import entity_registry as er

    config = MockConfigEntry(domain=DOMAIN, version=3, data=DATA)
    config.add_to_hass(hass)
    with patch(
        "custom_components.kepco_on.dday.dt_util.now", return_value=datetime(2026, 1, 4, tzinfo=UTC)
    ):
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
        entities = er.async_entries_for_config_entry(er.async_get(hass), config.entry_id)
        assert len(entities) == 3
        assert {hass.states.get(item.entity_id).state for item in entities} == {
            "D-1",
            "1",
            "2026-01-05",
        }
    display_id = next(item.entity_id for item in entities if item.unique_id.endswith("_display"))
    display = hass.data["entity_components"]["sensor"].get_entity(display_id)
    for day, state in [(5, "D-Day"), (6, "D+1")]:
        now = datetime(2026, 1, day, tzinfo=UTC)
        with patch("custom_components.kepco_on.dday.dt_util.now", return_value=now):
            display.async_midnight(now)
            assert hass.states.get(display_id).state == state
            assert display.extra_state_attributes["yearly"] is False
    diagnostics = await async_get_config_entry_diagnostics(hass, config)
    assert DATA["event_name"] not in str(diagnostics)
    options = KepcoOnConfigFlow.async_get_options_flow(config)
    assert isinstance(options, DdayOptionsFlow)
    options.hass, options.handler = hass, config.entry_id
    assert (await options.async_step_init())["step_id"] == "dday_options"
    assert (await options.async_step_dday_options({**DATA, "target_date": "bad"}))["errors"]
    with patch(
        "custom_components.kepco_on.dday.dt_util.now", return_value=datetime(2026, 1, 4, tzinfo=UTC)
    ):
        result = await options.async_step_dday_options(
            {**DATA, "event_name": "변경 일정", "target_date": "2026-01-14"}
        )
        await hass.async_block_till_done()
        assert result["type"] == "create_entry"
        assert hass.states.get(display_id).state == "D-10"
    assert config.title == "변경 일정"
    assert await hass.config_entries.async_unload(config.entry_id)
    await hass.async_block_till_done()


async def test_setup_flow_and_reconfigure() -> None:
    from tests.test_config_flow import FakeConfigEntry, make_flow

    flow = make_flow()
    assert (await flow.async_step_dday())["step_id"] == "dday"
    assert (await flow.async_step_dday({**DATA, "target_date": "bad"}))["errors"] == {
        "base": "dday_invalid_date"
    }
    result = await flow.async_step_dday(DATA)
    assert result["data"] == DATA
    entry = FakeConfigEntry(data=DATA, unique_id="LOCAL-DDAY-TEST")
    flow.hass.config_entries.entries_by_id[entry.entry_id] = entry
    flow.hass.config_entries.async_reload = AsyncMock()
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    assert (await flow.async_step_reconfigure())["step_id"] == "dday"
    assert (await flow.async_step_dday({**DATA, "yearly": True}))[
        "reason"
    ] == "reconfigure_successful"
    assert entry.data["yearly"] is True
    flow.context["source"] = "reauth"
    assert (await flow.async_step_reauth(entry.data))["step_id"] == "dday"
