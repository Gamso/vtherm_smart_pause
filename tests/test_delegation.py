"""Start/stop threshold: VTherm's own auto start/stop decides on the eco side of the mode's threshold."""

from conftest import FakeRuntime, make_entry
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vtherm_smart_pause.const import CONF_THRESHOLD_COOL, CONF_THRESHOLD_HEAT, VTHERM_DOMAIN
from custom_components.vtherm_smart_pause.decision import State
from custom_components.vtherm_smart_pause.manager import SmartPauseFeatureManager

SWITCH = "switch.salon_enable_auto_start_stop"


def _native_switch(hass, state: str = "off") -> list:
    """Register VTherm's auto start/stop switch and fake switch services. Return the calls."""
    vtherm_entry = MockConfigEntry(domain=VTHERM_DOMAIN, entry_id="vtherm-salon", data={"thermostat_type": "thermostat_over_climate"})
    vtherm_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create("switch", VTHERM_DOMAIN, "Salon_enable_auto_start_stop", suggested_object_id="salon_enable_auto_start_stop", config_entry=vtherm_entry)
    hass.states.async_set(SWITCH, state)
    calls = []

    async def handler(call):
        """Record the call and apply it to the state."""
        calls.append(call.service)
        hass.states.async_set(SWITCH, "on" if call.service == "turn_on" else "off")

    for service in ("turn_on", "turn_off"):
        hass.services.async_register("switch", service, handler)
    return calls


async def _manager(hass, **data) -> tuple[SmartPauseFeatureManager, FakeRuntime]:
    """Build a loaded manager on *data* (a 21 °C heating threshold by default: a 20 °C setpoint is eco)."""
    make_entry(**{CONF_THRESHOLD_HEAT: 21.0, **data}).add_to_hass(hass)
    hass.states.async_set("climate.salon", "heat", {"min_temp": 16.0, "max_temp": 30.0})
    runtime = FakeRuntime(hass)
    manager = SmartPauseFeatureManager(runtime, hass)
    manager.post_init(runtime.entry_infos)
    await manager.start_listening()
    return manager, runtime


async def _cycle(hass, manager, runtime, count: int = 1) -> None:
    """Run *count* one-minute cycles and let the service calls land."""
    for _ in range(count):
        await manager.refresh_state()
        await hass.async_block_till_done()
        runtime.advance(1)


async def test_below_the_heat_threshold_vtherm_decides(integration):
    """Below the heating threshold, VTherm's switch is turned on and the smart pause stands by."""
    calls = _native_switch(integration)
    manager, runtime = await _manager(integration)
    runtime.current_temperature = 20.4
    await _cycle(integration, manager, runtime, 15)
    assert calls == ["turn_on"]
    assert manager.decision.reason == "delegated"
    assert manager.engine.state == State.INACTIVE


async def test_above_the_heat_threshold_smart_pause_decides(integration):
    """Moving the setpoint up to the threshold turns VTherm's switch off and the smart pause decides."""
    calls = _native_switch(integration)
    manager, runtime = await _manager(integration)
    await _cycle(integration, manager, runtime)
    runtime.target_temperature = 21.0
    await _cycle(integration, manager, runtime)
    assert calls == ["turn_on", "turn_off"]
    assert manager.decision.reason != "delegated"


async def test_cool_threshold_mirrors_heat(integration):
    """In cooling, VTherm decides above the threshold, the smart pause up to it."""
    calls = _native_switch(integration)
    manager, runtime = await _manager(integration, **{CONF_THRESHOLD_HEAT: None, CONF_THRESHOLD_COOL: 26.0})
    runtime.vtherm_hvac_mode = runtime.requested_hvac_mode = "cool"
    runtime.target_temperature = 28.0
    await _cycle(integration, manager, runtime)
    assert manager.decision.reason == "delegated"
    runtime.target_temperature = 25.0
    await _cycle(integration, manager, runtime)
    assert calls == ["turn_on", "turn_off"]


async def test_threshold_is_per_mode(integration):
    """A heating threshold alone never delegates in cooling."""
    calls = _native_switch(integration)
    manager, runtime = await _manager(integration)
    runtime.vtherm_hvac_mode = runtime.requested_hvac_mode = "cool"
    runtime.target_temperature = 28.0
    await _cycle(integration, manager, runtime)
    assert not calls
    assert manager.decision.reason != "delegated"


async def test_side_is_read_on_the_requested_mode(integration):
    """With the equipment switched off by VTherm, the eco side still holds through the requested mode."""
    _native_switch(integration)
    manager, runtime = await _manager(integration)
    runtime.vtherm_hvac_mode = "off"
    runtime.hvac_off_reason = "hvac_off_auto_start_stop"
    await _cycle(integration, manager, runtime)
    assert manager.decision.reason == "delegated"


async def test_manual_flip_holds_until_next_transition(integration):
    """A switch turned back on by hand is left alone until the side changes."""
    calls = _native_switch(integration, state="on")
    manager, runtime = await _manager(integration)
    runtime.target_temperature = 21.0
    await _cycle(integration, manager, runtime)
    assert calls == ["turn_off"]
    integration.states.async_set(SWITCH, "on")
    await _cycle(integration, manager, runtime, 3)
    assert calls == ["turn_off"]


async def test_without_threshold_the_native_switch_is_off(integration):
    """Without a threshold, VTherm's auto start/stop never decides: one decider only."""
    calls = _native_switch(integration, state="on")
    manager, runtime = await _manager(integration, **{CONF_THRESHOLD_HEAT: None})
    await _cycle(integration, manager, runtime)
    assert calls == ["turn_off"]


async def test_threshold_without_the_feature_falls_back(integration):
    """A threshold on a thermostat without the auto start/stop feature: the smart pause decides, eco profile."""
    manager, runtime = await _manager(integration)
    runtime.current_temperature = 20.4
    await _cycle(integration, manager, runtime, 15)
    assert manager.engine.state == State.PAUSED
    assert manager.decision.profile == "eco"


async def test_disabled_plugin_leaves_the_switch_alone(integration):
    """With the smart pause switched off, VTherm's switch is not touched."""
    calls = _native_switch(integration, state="on")
    manager, runtime = await _manager(integration)
    await manager.async_set_enabled(False)
    await _cycle(integration, manager, runtime, 2)
    assert not calls
