"""Verify planner diagnostics through real HA state writing, not just properties."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import pytest
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.template import Template

from .test_apartment_planner import customer
from .test_sensor import bill as synthetic_bill
from .test_sensor import by_key, setup_entities

KEYS = ("current_period_usage", "predicted_period_usage", "monthly_usage")


async def publish(hass: HomeAssistant, entities: list[Any]) -> dict[str, Any]:
    """Add actual sensor entities to an HA entity platform and write initial state."""
    sensors = by_key(entities)
    selected = [sensors[key] for key in KEYS]
    for key, entity in zip(KEYS, selected, strict=True):
        entity.entity_id = f"sensor.kepco_publication_{key}"
    component = EntityComponent[SensorEntity](logging.getLogger(__name__), "sensor", hass)
    await component.async_add_entities(selected)
    await hass.async_block_till_done()
    return sensors


@pytest.mark.parametrize(
    ("status", "current", "code"),
    [
        ("no_data", None, "90"),
        ("no_data", None, "00"),
        ("no_data", None, None),
        ("not_requested", None, None),
        ("connection_error", None, None),
        ("rate_limited", None, None),
        ("invalid_response", None, "00"),
        ("ok", 0.0, "00"),
        ("ok", 246.8, "00"),
    ],
)
async def test_planner_attributes_are_published_to_states_and_template(
    hass: HomeAssistant, status: str, current: float | None, code: str | None
) -> None:
    item = customer()
    bill = replace(
        synthetic_bill(),
        current_period_usage_kwh=current,
        power_planner_status=status,
        power_planner_return_code=code,
    )
    sensors = await publish(
        hass,
        await setup_entities(
            customers=(item,),
            bills_by_customer_key={item.stable_key: bill},
        ),
    )
    for key, field in (
        ("current_period_usage", "F_AP_QT"),
        ("predicted_period_usage", "PREDICT_TOT"),
    ):
        state = hass.states.get(sensors[key].entity_id)
        assert state is not None
        expected = (
            "no_data"
            if key == "predicted_period_usage"
            and (status == "ok" or (status == "no_data" and code == "00"))
            else status
        )
        assert state.attributes["source_field"] == field
        assert state.attributes["data_status"] == expected
        assert state.attributes["return_code"] == code
        assert state.attributes["provider_return_code"] == code
        assert state.attributes["integration_version"] == "1.1.1"
        assert state.attributes["data_status_message"]
        assert state.attributes["unit_of_measurement"] == "kWh"
        assert "TEST_BUILDING" not in str(state.attributes)
        assert "TEST_HOUSEHOLD" not in str(state.attributes)
        assert "billing_month" not in state.attributes
        if key == "current_period_usage" and current is not None:
            assert float(state.state) == pytest.approx(current)
        else:
            assert state.state == STATE_UNKNOWN
    monthly = hass.states.get(sensors["monthly_usage"].entity_id)
    assert monthly is not None
    assert monthly.state == str(bill.usage_kwh)
    template = Template(
        "{% for s in states.sensor if s.attributes.get('source_field') in "
        "['F_AP_QT', 'PREDICT_TOT'] %}{{ s.entity_id }}\n{% else %}not_found{% endfor %}",
        hass,
    )
    rendered = template.async_render(parse_result=False)
    assert "not_found" not in rendered
    for key in KEYS[:2]:
        assert sensors[key].entity_id in rendered


@pytest.mark.parametrize("failure", ["coordinator", "customer", "missing_bill"])
async def test_real_billing_failure_still_marks_all_sensors_unavailable(
    hass: HomeAssistant, failure: str
) -> None:
    item = customer()
    entities = await setup_entities(
        customers=(item,),
        bills_by_customer_key={}
        if failure == "missing_bill"
        else {item.stable_key: synthetic_bill()},
        errors_by_customer_key={item.stable_key: "cannot_connect"} if failure == "customer" else {},
    )
    if failure == "coordinator":
        for entity in entities:
            entity.coordinator.last_update_success = False
    sensors = await publish(hass, entities)
    for key in KEYS:
        state = hass.states.get(sensors[key].entity_id)
        assert state is not None
        assert state.state == STATE_UNAVAILABLE


async def test_diagnostics_survive_value_loss_and_return_after_billing_recovery(
    hass: HomeAssistant,
) -> None:
    item = customer()
    initial = replace(synthetic_bill(), current_period_usage_kwh=0.0, power_planner_status="ok")
    sensors = await publish(
        hass,
        await setup_entities(
            customers=(item,),
            bills_by_customer_key={item.stable_key: initial},
        ),
    )
    coordinator = sensors["current_period_usage"].coordinator
    coordinator.data = replace(
        coordinator.data,
        bills_by_customer_key={
            item.stable_key: replace(
                initial,
                current_period_usage_kwh=None,
                power_planner_status="no_data",
                power_planner_return_code="90",
            )
        },
    )
    for key in KEYS:
        sensors[key].async_write_ha_state()
    missing = hass.states.get(sensors["current_period_usage"].entity_id)
    assert missing is not None
    assert missing.state == STATE_UNKNOWN
    assert missing.attributes["provider_return_code"] == "90"
    coordinator.last_update_success = False
    for key in KEYS:
        sensors[key].async_write_ha_state()
    failed = hass.states.get(sensors["current_period_usage"].entity_id)
    assert failed is not None
    assert failed.state == STATE_UNAVAILABLE
    coordinator.last_update_success = True
    for key in KEYS:
        sensors[key].async_write_ha_state()
    recovered = hass.states.get(sensors["current_period_usage"].entity_id)
    assert recovered is not None
    assert recovered.state == STATE_UNKNOWN
    assert recovered.attributes["source_field"] == "F_AP_QT"
    assert recovered.attributes["data_status"] == "no_data"
