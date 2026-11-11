"""Switch platform: turn the smart pause on or off for one VTherm."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_TARGET_VTHERM, PLATFORM_SWITCH
from .entity import SmartPauseEntity
from .registry import add_entities_registry, entity_bucket, get_manager


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    """Park the platform callback. The manager builds the switch when it is up."""
    target = entry.data.get(CONF_TARGET_VTHERM)
    if not target:
        return
    add_entities_registry(hass).setdefault(target, {})[PLATFORM_SWITCH] = async_add_entities
    manager = get_manager(hass, target)
    if manager is not None:
        manager.ensure_entities()


def build_entities(manager) -> list[SwitchEntity]:
    """Create the switch if not created yet."""
    bucket = entity_bucket(manager.hass, manager.vtherm_unique_id)
    if bucket.get(PLATFORM_SWITCH):
        return []
    bucket[PLATFORM_SWITCH] = [SmartPauseSwitch(manager)]
    return bucket[PLATFORM_SWITCH]


class SmartPauseSwitch(SmartPauseEntity, SwitchEntity):
    """On: the plugin may pause the equipment. Off: a pause ends at the next cycle."""

    def __init__(self, manager) -> None:
        super().__init__(manager, "enabled")

    @property
    def is_on(self) -> bool:
        """Whether the smart pause is enabled."""
        return self._manager.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the smart pause."""
        await self._manager.async_set_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the smart pause. VTherm takes the equipment back at its next cycle."""
        await self._manager.async_set_enabled(False)
        self.async_write_ha_state()
