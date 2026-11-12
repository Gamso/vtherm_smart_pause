"""Sensor platform: what the smart pause decided and what it learned."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_TARGET_VTHERM, PLATFORM_SENSOR
from .decision import DRIVEN_HVAC_MODES, HVAC_HEAT, State
from .entity import SmartPauseEntity
from .registry import add_entities_registry, entity_bucket, get_manager


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    """Park the platform callback. The manager builds the sensors when it is up.

    Home Assistant sets this platform up when the config entry loads, while
    VTherm creates the manager on its own schedule: whichever side comes second
    builds the entities.
    """
    target = entry.data.get(CONF_TARGET_VTHERM)
    if not target:
        return
    add_entities_registry(hass).setdefault(target, {})[PLATFORM_SENSOR] = async_add_entities
    manager = get_manager(hass, target)
    if manager is not None:
        manager.ensure_entities()


def build_entities(manager) -> list[SensorEntity]:
    """Create the sensors not created yet."""
    bucket = entity_bucket(manager.hass, manager.vtherm_unique_id)
    if bucket.get(PLATFORM_SENSOR):
        return []
    entities: list[SensorEntity] = [
        PauseStateSensor(manager),
        AutonomySensor(manager),
        HeatLossSensor(manager),
        PausesTodaySensor(manager),
    ]
    bucket[PLATFORM_SENSOR] = entities
    return entities


def _mode(manager) -> str:
    """The HVAC mode to read the model in: the VTherm's, heat when it is neither heat nor cool."""
    mode = manager.vtherm.vtherm_hvac_mode
    return mode if mode in DRIVEN_HVAC_MODES else HVAC_HEAT


class PauseStateSensor(SmartPauseEntity, SensorEntity):
    """Running, paused or inactive, with the reason."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [str(state) for state in State]

    def __init__(self, manager) -> None:
        super().__init__(manager, "state")

    @property
    def native_value(self) -> str:
        """The engine state."""
        return str(self._manager.engine.state)

    @property
    def extra_state_attributes(self) -> dict:
        """Why, in which profile, against which limit."""
        decision = self._manager.decision
        if decision is None:
            return {}
        return {"reason": decision.reason, "profile": decision.profile, "limit": decision.limit}


class AutonomySensor(SmartPauseEntity, SensorEntity):
    """Minutes the room would hold, equipment paused, before its comfort limit."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager) -> None:
        super().__init__(manager, "autonomy")

    @property
    def native_value(self) -> int | None:
        """The last predicted autonomy."""
        decision = self._manager.decision
        if decision is None or decision.autonomy_minutes is None:
            return None
        return round(decision.autonomy_minutes)


class HeatLossSensor(SmartPauseEntity, SensorEntity):
    """The learned heat-loss coefficient k."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "1/h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    def __init__(self, manager) -> None:
        super().__init__(manager, "heat_loss")

    @property
    def native_value(self) -> float:
        """k in the VTherm's current HVAC mode."""
        return round(self._manager.model.heat_loss(_mode(self._manager)), 4)

    @property
    def extra_state_attributes(self) -> dict:
        """Solar gain, learned segments, readiness and recovery time."""
        model, mode = self._manager.model, _mode(self._manager)
        return {
            "solar_gain": round(model.solar_gain(mode), 3),
            "segments": len(model.segments),
            "sunless_segments": model.sunless_count(mode),
            "ready": model.is_ready(mode),
            "recovery_minutes": round(model.recovery_minutes(mode), 1),
        }


class PausesTodaySensor(SmartPauseEntity, SensorEntity):
    """Number of pauses since midnight."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager) -> None:
        super().__init__(manager, "pauses_today")

    @property
    def native_value(self) -> int:
        """The daily pause count."""
        return self._manager.engine.pauses_today
