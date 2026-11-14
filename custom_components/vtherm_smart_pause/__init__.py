"""The vtherm_smart_pause integration.

A Versatile Thermostat feature-manager plugin that pauses the equipment of an
``over_climate`` thermostat while the room does not need it and will hold
without it. This module only wires the plugin into VTherm: it registers the
factory and sets up the entity platforms. The cycle lives in :mod:`manager`,
the decision in :mod:`decision` and the learned model in :mod:`thermal_model`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, Platform
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .const import (
    CONF_TARGET_VTHERM,
    CONF_THERMOSTAT_CLIMATE,
    CONF_THERMOSTAT_TYPE,
    DATA_FACTORY_REGISTERED,
    DOMAIN,
    FEATURE_MANAGER_NAME,
    STORAGE_KEY,
    STORAGE_VERSION,
    VTHERM_DOMAIN,
)
from .factory import SmartPauseManagerFactory
from .manager import native_switch
from .log import get_logger
from .registry import clear_registry, domain_data

_LOGGER = get_logger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)  # pylint: disable=invalid-name  # name required by HA


def _register_factory(hass: HomeAssistant) -> bool:
    """Register the factory with the shared VTherm API."""
    data = domain_data(hass)
    if data.get(DATA_FACTORY_REGISTERED) is True:
        return True
    # Imported here: vtherm_api ships with Versatile Thermostat, not with this
    # plugin, and a missing one must give this message rather than a traceback.
    try:
        from vtherm_api.vtherm_api import VThermAPI  # pylint: disable=import-outside-toplevel
    except ImportError:
        _LOGGER.error("vtherm_api is not available. Install Versatile Thermostat >= 10.6.4, which provides it")
        return False
    api = VThermAPI.get_vtherm_api(hass)
    if api is None:
        _LOGGER.warning("VThermAPI unavailable. The smart pause factory is not registered yet")
        return False
    factory = SmartPauseManagerFactory(hass)
    if api.get_feature_manager(factory.name) is None:
        api.register_feature_manager(factory)
    data[DATA_FACTORY_REGISTERED] = True
    return True


def _unregister_factory(hass: HomeAssistant) -> None:
    """Unregister the factory from the shared VTherm API."""
    try:
        from vtherm_api.vtherm_api import VThermAPI  # pylint: disable=import-outside-toplevel
    except ImportError:
        return
    api = VThermAPI.get_vtherm_api(hass)
    if api is not None:
        api.unregister_feature_manager(FEATURE_MANAGER_NAME)
    domain_data(hass)[DATA_FACTORY_REGISTERED] = False


async def _reload_target_vtherm(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the targeted VTherm so it creates (or drops) the manager.

    A VTherm builds its feature managers at setup. Its unique_id, which this
    plugin stores, is its config entry's entry_id.
    """
    target = entry.data.get(CONF_TARGET_VTHERM)
    tasks = [
        hass.config_entries.async_reload(vtherm_entry.entry_id)
        for vtherm_entry in hass.config_entries.async_entries(VTHERM_DOMAIN)
        if vtherm_entry.entry_id == target and vtherm_entry.data.get(CONF_THERMOSTAT_TYPE) == CONF_THERMOSTAT_CLIMATE
    ]
    if tasks:
        await asyncio.gather(*tasks)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the factory as early as possible, before VTherm sets its thermostats up."""
    del config
    _register_factory(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one smart pause from its config entry."""
    domain_data(hass)[entry.entry_id] = entry.entry_id
    _register_factory(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    # At startup VTherm sets its own entries up independently. Reloading here would race with it.
    if hass.state == CoreState.running:
        await _reload_target_vtherm(hass, entry)
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so changed options reach the manager."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one smart pause."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    target = entry.data.get(CONF_TARGET_VTHERM)
    if target:
        clear_registry(hass, target)
    data = domain_data(hass)
    data.pop(entry.entry_id, None)
    if not [other for other in hass.config_entries.async_loaded_entries(DOMAIN) if other.entry_id != entry.entry_id]:
        _unregister_factory(hass)
    # The VTherm still holds this manager (and maybe a pause): reload it so it drops both.
    if hass.state == CoreState.running:
        await _reload_target_vtherm(hass, entry)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Hand VTherm's auto start/stop back as its owner had it, and delete what the plugin learned.

    Called once the entry is unloaded, so no manager writes the switch again.
    """
    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
    data = await store.async_load() or {}
    before = data.get("native_before")
    target = entry.data.get(CONF_TARGET_VTHERM)
    switch = native_switch(hass, target) if target else None
    state = hass.states.get(switch) if switch else None
    if isinstance(before, bool) and state is not None and state.state in (STATE_ON, STATE_OFF) and (state.state == STATE_ON) != before:
        _LOGGER.info("Restoring %s to %s, as it was before the smart pause", switch, "on" if before else "off")
        await hass.services.async_call("switch", "turn_on" if before else "turn_off", {"entity_id": switch}, blocking=True)
    await store.async_remove()
