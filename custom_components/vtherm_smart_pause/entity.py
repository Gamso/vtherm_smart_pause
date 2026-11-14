"""Base entity shared by the sensor and switch platforms."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DEVICE_NAME, DOMAIN, build_unique_id

if TYPE_CHECKING:
    from .manager import SmartPauseFeatureManager


class SmartPauseEntity(Entity):
    """An entity of one VTherm's smart pause, attached to its own device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: "SmartPauseFeatureManager", key: str) -> None:
        self._manager = manager
        self._attr_translation_key = key
        self._attr_unique_id = build_unique_id(key, manager.entry_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, manager.entry_id)},
            name=f"{manager.vtherm.name} Smart Pause",
            model=DEVICE_NAME,
        )

    def bind(self, manager: "SmartPauseFeatureManager") -> None:
        """Read from *manager* from now on: the one VTherm created when the thermostat reloaded."""
        self._manager = manager
