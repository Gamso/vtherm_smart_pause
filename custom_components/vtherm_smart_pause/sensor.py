"""Sensor platform: what the smart pause decided and what it learned."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
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
        SolarGainSensor(manager),
        RecoveryTimeSensor(manager),
        ModelErrorSensor(manager),
        PausesTodaySensor(manager),
    ]
    bucket[PLATFORM_SENSOR] = entities
    return entities


def _mode(manager) -> str:
    """The HVAC mode to read the model in: the VTherm's, heat when it is neither heat nor cool."""
    mode = manager.hvac_mode
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
        """Solar gain, learned segments, their spread, readiness and recovery time."""
        model, mode = self._manager.model, _mode(self._manager)
        spread = model.heat_loss_range(mode)
        return {
            "k_min": round(spread[0], 4) if spread else None,
            "k_max": round(spread[1], 4) if spread else None,
            "solar_gain": round(model.solar_gain(mode), 3),
            "segments": len(model.segments),
            "sunless_segments": model.sunless_count(mode),
            "ready": model.is_ready(mode),
            "segments_from_history": self._manager.history_segments,
            "recovery_minutes": round(model.recovery_minutes(mode), 1),
        }


class SolarGainSensor(SmartPauseEntity, SensorEntity):
    """The learned solar gain g: how fast full sun warms the room."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "°C/h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, manager) -> None:
        super().__init__(manager, "solar_gain")

    @property
    def native_value(self) -> float:
        """g in the VTherm's current HVAC mode, 0 until enough sunny stops are learned."""
        return round(self._manager.model.solar_gain(_mode(self._manager)), 3)


class RecoveryTimeSensor(SmartPauseEntity, SensorEntity):
    """Minutes the room keeps drifting after the equipment restarts."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, manager) -> None:
        super().__init__(manager, "recovery_time")

    @property
    def native_value(self) -> float:
        """The median measured recovery, the default one until a restart was measured."""
        return round(self._manager.model.recovery_minutes(_mode(self._manager)), 1)

    @property
    def extra_state_attributes(self) -> dict:
        """How many restarts the value comes from (0: it is the default)."""
        return {"measured_restarts": len(self._manager.model.recoveries.get(_mode(self._manager)) or [])}


class ModelErrorSensor(SmartPauseEntity, SensorEntity):
    """How far the room ended from what the model predicted, over the last pauses."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, manager) -> None:
        super().__init__(manager, "model_error")

    @property
    def native_value(self) -> float | None:
        """Median absolute error (°C), unknown until a pause was evaluated."""
        error = self._manager.model.prediction_error(_mode(self._manager))
        return None if error is None else round(error, 2)

    @property
    def extra_state_attributes(self) -> dict:
        """The last error and the bias, both measured - predicted, and the number of pauses behind them."""
        model, mode = self._manager.model, _mode(self._manager)
        errors = model.errors.get(mode) or []
        bias = model.prediction_bias(mode)
        return {
            "last_error": round(errors[-1], 2) if errors else None,
            "bias": round(bias, 2) if bias is not None else None,
            "evaluated_pauses": len(errors),
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
