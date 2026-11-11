"""Factory registering the smart pause feature manager with the VTherm API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .const import CONF_TARGET_VTHERM, CONF_THERMOSTAT_CLIMATE, CONF_THERMOSTAT_TYPE, DOMAIN, FEATURE_MANAGER_NAME
from .log import get_logger
from .manager import SmartPauseFeatureManager

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from vtherm_api.interfaces import InterfaceFeatureManager, InterfaceThermostatRuntime

_LOGGER = get_logger(__name__)


class SmartPauseManagerFactory:
    """Create smart pause managers for the VTherms this plugin is configured for.

    Two conditions: the thermostat is an ``over_climate`` (only those send a
    setpoint the plugin can lower), and a config entry targets that very VTherm
    -- the plugin stays dormant on thermostats its owner did not opt in.

    It must be registered with ``register_feature_manager``: VTherm asks only the
    managers its factories created to adjust the regulated temperature.
    """

    def __init__(self, hass: "HomeAssistant") -> None:
        self._hass = hass

    @property
    def name(self) -> str:
        """Return the feature manager identifier."""
        return FEATURE_MANAGER_NAME

    def supports(self, thermostat: "InterfaceThermostatRuntime") -> bool:
        """Return True when this thermostat is an opted-in over_climate VTherm."""
        try:
            entry_infos = thermostat.entry_infos
        except Exception:  # pylint: disable=broad-except
            return False
        if not isinstance(entry_infos, Mapping) or entry_infos.get(CONF_THERMOSTAT_TYPE) != CONF_THERMOSTAT_CLIMATE:
            return False
        return self._is_configured_for(thermostat)

    def _is_configured_for(self, thermostat: "InterfaceThermostatRuntime") -> bool:
        """Return True when a config entry targets this VTherm."""
        config_entries = getattr(self._hass, "config_entries", None)
        if config_entries is None:
            return False
        try:
            entries = config_entries.async_entries(DOMAIN)
        except Exception:  # pylint: disable=broad-except
            return False
        return any(entry.data.get(CONF_TARGET_VTHERM) == thermostat.unique_id for entry in entries)

    def create(self, thermostat: "InterfaceThermostatRuntime") -> "InterfaceFeatureManager":
        """Create a manager bound to the runtime thermostat."""
        _LOGGER.info("Creating smart pause manager for VTherm %s", thermostat.name)
        return SmartPauseFeatureManager(thermostat, thermostat.hass)
