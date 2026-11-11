"""Config and options flow for VTherm Smart Pause."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_COMFORT_EXCESS,
    CONF_COMFORT_EXCESS_MINUTES,
    CONF_COMFORT_MARGIN,
    CONF_COMFORT_MIN_PAUSE,
    CONF_COMPRESSOR_ENTITY,
    CONF_DEFROST_ENTITY,
    CONF_ECO_EXCESS,
    CONF_ECO_EXCESS_MINUTES,
    CONF_ECO_MARGIN,
    CONF_ECO_MIN_PAUSE,
    CONF_MAX_PAUSES_PER_DAY,
    CONF_MIN_OFF_MINUTES,
    CONF_MIN_RUN_MINUTES,
    CONF_OUTDOOR_CEILING_COOL,
    CONF_OUTDOOR_FLOOR_HEAT,
    CONF_POWER_ENTITY,
    CONF_SETBACK_COOL,
    CONF_SETBACK_HEAT,
    CONF_TARGET_VTHERM,
    CONF_THERMOSTAT_CLIMATE,
    CONF_THERMOSTAT_TYPE,
    CONF_THRESHOLD_COOL,
    CONF_THRESHOLD_HEAT,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    VTHERM_DOMAIN,
)
from .decision import Settings
from .registry import merged_config

_DEFAULTS = Settings()


def _temperature(minimum: float, maximum: float, step: float = 0.5) -> selector.NumberSelector:
    """A temperature box."""
    return selector.NumberSelector(selector.NumberSelectorConfig(min=minimum, max=maximum, step=step, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX))


def _minutes(maximum: float) -> selector.NumberSelector:
    """A duration box in minutes."""
    return selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=maximum, step=1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX))


def _optional(key: str, defaults: dict[str, Any]) -> vol.Optional:
    """An optional field pre-filled only when it has a stored value."""
    value = defaults.get(key)
    return vol.Optional(key, description={"suggested_value": value} if value is not None else None)


def _entities_schema(defaults: dict[str, Any]) -> dict:
    """Thresholds and optional entities, shared by both flows."""
    return {
        _optional(CONF_THRESHOLD_HEAT, defaults): _temperature(5, 35),
        _optional(CONF_THRESHOLD_COOL, defaults): _temperature(15, 35),
        _optional(CONF_WEATHER_ENTITY, defaults): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
        _optional(CONF_POWER_ENTITY, defaults): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class=["power", "energy"])),
        _optional(CONF_COMPRESSOR_ENTITY, defaults): selector.EntitySelector(selector.EntitySelectorConfig(domain=["binary_sensor", "sensor", "switch", "input_boolean"])),
        _optional(CONF_DEFROST_ENTITY, defaults): selector.EntitySelector(selector.EntitySelectorConfig(domain=["binary_sensor", "sensor", "input_boolean"])),
    }


def _tuning_schema(defaults: dict[str, Any]) -> dict:
    """Setback, profile margins and compressor guards (options only)."""

    def with_default(key: str, value: float) -> vol.Optional:
        return vol.Optional(key, default=defaults.get(key, value))

    comfort, eco = _DEFAULTS.comfort, _DEFAULTS.eco
    margin = selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=3, step=0.1, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX))
    return {
        _optional(CONF_SETBACK_HEAT, defaults): _temperature(5, 25),
        _optional(CONF_SETBACK_COOL, defaults): _temperature(20, 35),
        with_default(CONF_COMFORT_EXCESS, comfort.excess): margin,
        with_default(CONF_COMFORT_EXCESS_MINUTES, comfort.excess_minutes): _minutes(240),
        with_default(CONF_COMFORT_MARGIN, comfort.margin): margin,
        with_default(CONF_COMFORT_MIN_PAUSE, comfort.min_pause_minutes): _minutes(360),
        with_default(CONF_ECO_EXCESS, eco.excess): margin,
        with_default(CONF_ECO_EXCESS_MINUTES, eco.excess_minutes): _minutes(240),
        with_default(CONF_ECO_MARGIN, eco.margin): margin,
        with_default(CONF_ECO_MIN_PAUSE, eco.min_pause_minutes): _minutes(360),
        with_default(CONF_MIN_RUN_MINUTES, _DEFAULTS.min_run_minutes): _minutes(240),
        with_default(CONF_MIN_OFF_MINUTES, _DEFAULTS.min_off_minutes): _minutes(240),
        with_default(CONF_MAX_PAUSES_PER_DAY, _DEFAULTS.max_pauses_per_day): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=48, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        with_default(CONF_OUTDOOR_FLOOR_HEAT, _DEFAULTS.outdoor_floor_heat): _temperature(-30, 20),
        with_default(CONF_OUTDOOR_CEILING_COOL, _DEFAULTS.outdoor_ceiling_cool): _temperature(20, 50),
    }


def _vtherm_entry(hass, unique_id: str):
    """Return the VTherm config entry of a thermostat (its unique_id is its entry_id)."""
    for entry in hass.config_entries.async_entries(VTHERM_DOMAIN):
        if entry.entry_id == unique_id:
            return entry
    return None


class SmartPauseConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Attach a smart pause to one over_climate Versatile Thermostat."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the VTherm, the comfort thresholds and the optional entities."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id = user_input[CONF_TARGET_VTHERM]
            registry_entry = er.async_get(self.hass).async_get(entity_id)
            if registry_entry is None or registry_entry.platform != VTHERM_DOMAIN or registry_entry.unique_id is None:
                errors[CONF_TARGET_VTHERM] = "not_a_vtherm"
            else:
                vtherm_entry = _vtherm_entry(self.hass, registry_entry.unique_id)
                config = {**vtherm_entry.data, **vtherm_entry.options} if vtherm_entry is not None else {}
                if config.get(CONF_THERMOSTAT_TYPE) != CONF_THERMOSTAT_CLIMATE:
                    errors[CONF_TARGET_VTHERM] = "not_over_climate"
            if not errors:
                await self.async_set_unique_id(f"{DOMAIN}-{registry_entry.unique_id}")
                self._abort_if_unique_id_configured()
                state = self.hass.states.get(entity_id)
                data = {**user_input, CONF_TARGET_VTHERM: registry_entry.unique_id}
                return self.async_create_entry(title=state.name if state is not None else entity_id, data=data)

        schema = {
            vol.Required(CONF_TARGET_VTHERM): selector.EntitySelector(selector.EntitySelectorConfig(domain=CLIMATE_DOMAIN, integration=VTHERM_DOMAIN)),
            **_entities_schema(user_input or {}),
        }
        return self.async_show_form(step_id="user", data_schema=vol.Schema(schema), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "SmartPauseOptionsFlow":
        """Return the options flow. Home Assistant sets its config_entry itself."""
        del config_entry
        return SmartPauseOptionsFlow()


class SmartPauseOptionsFlow(OptionsFlow):
    """Edit thresholds, entities and tuning (the target VTherm is fixed)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and store the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = merged_config(self.config_entry)
        return self.async_show_form(step_id="init", data_schema=vol.Schema({**_entities_schema(current), **_tuning_schema(current)}))
