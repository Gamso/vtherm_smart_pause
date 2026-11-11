"""Tests of the feature manager against a real Home Assistant core and a fake VTherm runtime."""

import pytest
from conftest import FakeRuntime, make_entry

from custom_components.vtherm_smart_pause.const import (
    ATTR_SECTION,
    CONF_COMPRESSOR_ENTITY,
    CONF_POWER_ENTITY,
    CONF_SETBACK_HEAT,
    CONF_THRESHOLD_HEAT,
    HVAC_OFF_REASON_WINDOW,
)
from custom_components.vtherm_smart_pause.decision import State
from custom_components.vtherm_smart_pause.manager import SmartPauseFeatureManager


async def _manager(hass, **data) -> tuple[SmartPauseFeatureManager, FakeRuntime]:
    """Build a loaded manager on an eco-profile entry (threshold above the 20 °C setpoint)."""
    data.setdefault(CONF_THRESHOLD_HEAT, 21.0)
    make_entry(**data).add_to_hass(hass)
    hass.states.async_set("climate.salon", "heat", {"min_temp": 16.0, "max_temp": 30.0})
    runtime = FakeRuntime(hass)
    manager = SmartPauseFeatureManager(runtime, hass)
    manager.post_init(runtime.entry_infos)
    await manager.start_listening()
    return manager, runtime


async def _cycles(manager: SmartPauseFeatureManager, runtime: FakeRuntime, count: int, minutes: float = 1.0) -> None:
    """Run *count* cycles, *minutes* apart."""
    for _ in range(count):
        await manager.refresh_state()
        runtime.advance(minutes)


async def _paused(hass, **data) -> tuple[SmartPauseFeatureManager, FakeRuntime]:
    """Return a manager that has just paused a room 0.4 °C over its setpoint."""
    manager, runtime = await _manager(hass, **data)
    runtime.current_temperature = 20.4
    await _cycles(manager, runtime, 12)
    assert manager.engine.state == State.PAUSED
    return manager, runtime


async def test_pause_sends_the_setback(integration):
    """Once paused, the regulated temperature becomes the VTherm's minimum."""
    manager, _ = await _paused(integration)
    assert manager.is_detected
    assert manager.adjust_regulated_temperature(20.0, 0.5, None) == 16.0


async def test_configured_setback_wins(integration):
    """A configured setback replaces the VTherm's minimum."""
    manager, _ = await _paused(integration, **{CONF_SETBACK_HEAT: 12.0})
    assert manager.adjust_regulated_temperature(20.0, 0.5, None) == 12.0


async def test_no_adjustment_while_running_or_tracking(integration):
    """Running, or following a change made on the device, the setpoint is VTherm's."""
    manager, runtime = await _manager(integration)
    await _cycles(manager, runtime, 1)
    assert manager.adjust_regulated_temperature(20.0, 0.5, None) is None
    manager, _ = await _paused(integration)
    assert manager.adjust_regulated_temperature(20.0, 0.5, "underlying_tracking") is None


async def test_disable_releases_the_pause(integration):
    """Turning the switch off ends the pause at the next cycle."""
    manager, runtime = await _paused(integration)
    await manager.async_set_enabled(False)
    await _cycles(manager, runtime, 1)
    assert manager.engine.state == State.INACTIVE
    assert manager.adjust_regulated_temperature(20.0, 0.5, None) is None


async def test_window_releases_the_pause(integration):
    """A window that switched VTherm off makes the engine inactive."""
    manager, runtime = await _paused(integration)
    runtime.vtherm_hvac_mode = "off"
    runtime.hvac_off_reason = HVAC_OFF_REASON_WINDOW
    await _cycles(manager, runtime, 1)
    assert manager.decision.reason == "window"


async def test_idle_equipment_is_not_paused(integration):
    """A power sensor reading next to nothing blocks the pause."""
    integration.states.async_set("sensor.pac_power", "1", {"unit_of_measurement": "W"})
    manager, runtime = await _manager(integration, **{CONF_POWER_ENTITY: "sensor.pac_power"})
    runtime.current_temperature = 20.4
    await _cycles(manager, runtime, 15)
    assert manager.decision.reason == "nothing_to_save"


async def test_avoided_energy_accumulates(integration):
    """While paused, the power drawn before the pause is counted as avoided."""
    integration.states.async_set("sensor.pac_power", "300", {"unit_of_measurement": "W"})
    manager, runtime = await _paused(integration, **{CONF_POWER_ENTITY: "sensor.pac_power"})
    await _cycles(manager, runtime, 60)
    assert manager.avoided_energy_kwh == pytest.approx(0.3, abs=0.02)


async def test_learns_from_a_night_off(integration):
    """Three hours of VTherm off, room cooling, become one learned segment."""
    manager, runtime = await _manager(integration)
    runtime.vtherm_hvac_mode = "off"
    for minute in range(240):
        runtime.current_temperature = round(round((20.0 - 0.006 * minute) / 0.2) * 0.2, 1)
        await _cycles(manager, runtime, 1)
    runtime.vtherm_hvac_mode = "heat"
    await _cycles(manager, runtime, 1)
    assert len(manager.model.segments) == 1
    assert manager.model.segments[0].hvac_mode == "heat"


async def test_running_compressor_is_not_a_stop(integration):
    """A pause during which the compressor still runs teaches nothing."""
    integration.states.async_set("binary_sensor.compressor", "on")
    manager, runtime = await _paused(integration, **{CONF_COMPRESSOR_ENTITY: "binary_sensor.compressor"})
    assert manager._recorder is None  # noqa: SLF001


async def test_pause_survives_a_restart(integration):
    """A new manager on the same entry comes back paused."""
    manager, runtime = await _paused(integration)
    await manager.async_save()
    restored = SmartPauseFeatureManager(runtime, integration)
    await restored.start_listening()
    assert restored.engine.state == State.PAUSED
    assert restored.adjust_regulated_temperature(20.0, 0.5, None) == 16.0


async def test_custom_attributes(integration):
    """The VTherm attributes carry the state another plugin reads."""
    manager, _ = await _paused(integration)
    attributes: dict = {}
    manager.add_custom_attributes(attributes)
    section = attributes[ATTR_SECTION]
    assert section["state"] == "paused"
    assert section["profile"] == "eco"
    assert section["setback"] == 16.0
    assert section["model_ready"] is False
