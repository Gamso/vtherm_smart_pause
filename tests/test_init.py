"""Setup, unload, factory and entities against a real Home Assistant core."""

from conftest import FakeRuntime, make_entry
from vtherm_api.interfaces import InterfaceFeatureManager, InterfaceFeatureManagerFactory
from vtherm_api.setpoint_modifier import InterfaceSetpointModifier
from vtherm_api.vtherm_api import VThermAPI

from custom_components.vtherm_smart_pause.const import DOMAIN
from custom_components.vtherm_smart_pause.factory import SmartPauseManagerFactory
from custom_components.vtherm_smart_pause.registry import get_manager


async def test_setup_registers_and_unload_unregisters(integration):
    """The factory is registered with the VTherm API while an entry is loaded."""
    entry = make_entry()
    entry.add_to_hass(integration)
    assert await integration.config_entries.async_setup(entry.entry_id)
    await integration.async_block_till_done()
    api = VThermAPI.get_vtherm_api(integration)
    assert api.get_feature_manager(DOMAIN) is not None
    assert await integration.config_entries.async_unload(entry.entry_id)
    assert api.get_feature_manager(DOMAIN) is None


async def test_factory_honours_the_protocols(integration):
    """Factory and manager satisfy vtherm_api's runtime-checkable contracts, setpoint modifier included."""
    make_entry().add_to_hass(integration)
    factory = SmartPauseManagerFactory(integration)
    runtime = FakeRuntime(integration)
    assert isinstance(factory, InterfaceFeatureManagerFactory)
    assert factory.supports(runtime)
    manager = factory.create(runtime)
    assert isinstance(manager, InterfaceFeatureManager)
    assert isinstance(manager, InterfaceSetpointModifier)


async def test_factory_ignores_other_thermostats(integration):
    """No entry, or not an over_climate: no manager."""
    factory = SmartPauseManagerFactory(integration)
    assert not factory.supports(FakeRuntime(integration))
    make_entry().add_to_hass(integration)
    assert not factory.supports(FakeRuntime(integration, entry_infos={"thermostat_type": "thermostat_over_switch"}))


async def test_entities_are_created_by_the_manager(integration):
    """Once the platforms are up and the manager runs, the sensors and the switch exist."""
    entry = make_entry()
    entry.add_to_hass(integration)
    assert await integration.config_entries.async_setup(entry.entry_id)
    await integration.async_block_till_done()
    runtime = FakeRuntime(integration)
    manager = SmartPauseManagerFactory(integration).create(runtime)
    manager.post_init(runtime.entry_infos)
    assert get_manager(integration, runtime.unique_id) is manager
    await manager.refresh_state()
    await integration.async_block_till_done()
    assert integration.states.get("sensor.salon_smart_pause_state").state == "running"
    assert integration.states.get("switch.salon_smart_pause_smart_pause").state == "on"
    # The runtime is in heat: the model is read with VTherm's mode object, which is unhashable.
    heat_loss = integration.states.get("sensor.salon_smart_pause_heat_loss_coefficient")
    assert float(heat_loss.state) == 0.08
    assert heat_loss.attributes["recovery_minutes"] == 30.0
    assert manager.decision is not None and manager.decision.reason == "no_excess"

    await integration.services.async_call("switch", "turn_off", {"entity_id": "switch.salon_smart_pause_smart_pause"}, blocking=True)
    assert manager.enabled is False
