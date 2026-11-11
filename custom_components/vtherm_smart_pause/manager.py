"""The VTherm feature manager: one per thermostat, driven by VTherm's control cycle.

VTherm calls :meth:`SmartPauseFeatureManager.refresh_state` once per control
cycle. Each cycle reads the thermostat, learns from real stops, asks the
:class:`~.decision.DecisionEngine` whether to pause or resume, and publishes the
result. The pause itself goes through the setpoint-modifier contract of
``vtherm_api``: while paused, :meth:`adjust_regulated_temperature` returns the
setback temperature, so the equipment's own thermostat goes idle while it stays
on and VTherm stays in its HVAC mode. VTherm sends it on its next regulation,
so a pause or a resume takes effect within one cycle.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .activity import ActivityTracker
from .const import (
    ACTIVITY_MIN_POWER_W,
    ACTIVITY_WINDOW_S,
    ATTR_SECTION,
    COMMAND_CAUSE_UNDERLYING_TRACKING,
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
    CONF_THRESHOLD_COOL,
    CONF_THRESHOLD_HEAT,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    FALLBACK_SETBACK_COOL,
    FALLBACK_SETBACK_HEAT,
    FEATURE_MANAGER_NAME,
    HVAC_OFF_REASON_WINDOW,
    PLATFORM_SENSOR,
    PLATFORM_SWITCH,
    SAVE_INTERVAL_S,
    STORAGE_KEY,
    STORAGE_VERSION,
    VTHERM_CONF_AUTO_START_STOP,
)
from .decision import DRIVEN_HVAC_MODES, HVAC_COOL, HVAC_HEAT, Action, Decision, DecisionEngine, Inputs, Profile, Settings, State
from .forecast import WeatherForecast
from .log import get_logger, write_event_log
from .registry import add_entities_registry, managers, merged_config
from .thermal_model import RecoveryTracker, SegmentRecorder, SensorResolution, ThermalModel

if TYPE_CHECKING:
    from vtherm_api.interfaces import InterfaceThermostatRuntime

_LOGGER = get_logger(__name__)

_ON_STATES = {STATE_ON, "true", "running", "heating", "cooling", "defrosting"}


def settings_from_config(conf: dict[str, Any]) -> Settings:
    """Build the decision settings from a config entry's merged data and options."""
    defaults = Settings()

    def number(key: str, default: float) -> float:
        value = conf.get(key)
        return float(value) if isinstance(value, (int, float)) else default

    def optional(key: str) -> float | None:
        value = conf.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    comfort, eco = defaults.comfort, defaults.eco
    return Settings(
        comfort=Profile(
            excess=number(CONF_COMFORT_EXCESS, comfort.excess),
            excess_minutes=number(CONF_COMFORT_EXCESS_MINUTES, comfort.excess_minutes),
            margin=number(CONF_COMFORT_MARGIN, comfort.margin),
            min_pause_minutes=number(CONF_COMFORT_MIN_PAUSE, comfort.min_pause_minutes),
        ),
        eco=Profile(
            excess=number(CONF_ECO_EXCESS, eco.excess),
            excess_minutes=number(CONF_ECO_EXCESS_MINUTES, eco.excess_minutes),
            margin=number(CONF_ECO_MARGIN, eco.margin),
            min_pause_minutes=number(CONF_ECO_MIN_PAUSE, eco.min_pause_minutes),
        ),
        threshold_heat=optional(CONF_THRESHOLD_HEAT),
        threshold_cool=optional(CONF_THRESHOLD_COOL),
        min_run_minutes=number(CONF_MIN_RUN_MINUTES, defaults.min_run_minutes),
        min_off_minutes=number(CONF_MIN_OFF_MINUTES, defaults.min_off_minutes),
        max_pauses_per_day=int(number(CONF_MAX_PAUSES_PER_DAY, defaults.max_pauses_per_day)),
        outdoor_floor_heat=number(CONF_OUTDOOR_FLOOR_HEAT, defaults.outdoor_floor_heat),
        outdoor_ceiling_cool=number(CONF_OUTDOOR_CEILING_COOL, defaults.outdoor_ceiling_cool),
    )


class SmartPauseFeatureManager:  # pylint: disable=too-many-instance-attributes
    """Pause the equipment of one VTherm when the room does not need it."""

    #: Recorder can only filter top-level keys, so every attribute this manager
    #: publishes on the VTherm lives under one section, declared here.
    unrecorded_attributes = frozenset({ATTR_SECTION})

    def __init__(self, thermostat: "InterfaceThermostatRuntime", hass: HomeAssistant) -> None:
        self._vtherm = thermostat
        self._hass = hass
        self._name = thermostat.name
        self._conf: dict[str, Any] = {}
        self._entry_id: str = thermostat.unique_id
        self._store: Store | None = None
        self._loaded = False
        self._last_save = 0.0

        self.model = ThermalModel()
        self.engine = DecisionEngine()
        self.enabled = True
        self.avoided_energy_kwh = 0.0
        self._forecast: WeatherForecast | None = None
        self._activity = ActivityTracker(ACTIVITY_WINDOW_S, ACTIVITY_MIN_POWER_W)
        self._resolution = SensorResolution()
        self._recovery = RecoveryTracker()
        self._recorder: SegmentRecorder | None = None
        self._pause_power_w: float | None = None
        self._last_cycle: float | None = None
        self._decision: Decision | None = None
        self._listeners: list = []
        self._entity_listeners: list = []

    # ------------------------------------------------------------------
    # InterfaceFeatureManager contract
    # ------------------------------------------------------------------
    def post_init(self, entry_infos: Any) -> None:
        """Publish the manager so the entity platforms can find it."""
        if isinstance(entry_infos, dict) and entry_infos.get(VTHERM_CONF_AUTO_START_STOP):
            _LOGGER.warning(
                "%s - VTherm's own auto start/stop is enabled: it decides on its own when to switch the equipment off. "
                "Disable it in the VTherm configuration so that only one of them decides.",
                self,
            )
        managers(self._hass)[self._vtherm.unique_id] = self

    async def start_listening(self, force: bool = False) -> None:
        """Load the persisted model."""
        del force
        await self._async_ensure_loaded()

    def stop_listening(self) -> bool | None:
        """Remove every listener registered through :meth:`add_listener`."""
        while self._listeners:
            self._listeners.pop()()

    async def refresh_state(self) -> bool:
        """Run one cycle. Returns True when the pause state changed."""
        await self._async_ensure_loaded()
        self.ensure_entities()
        changed = await self._async_run_cycle(self._now())
        self.push_to_entities()
        return changed

    def restore_state(self, old_state: Any) -> None:
        """No-op: the model and the pause are restored from this plugin's own Store."""
        del old_state

    def add_listener(self, func) -> None:
        """Register a callback to be removed on stop."""
        self._listeners.append(func)

    @property
    def is_configured(self) -> bool:
        """True once loaded: VTherm only asks configured managers to adjust its setpoint."""
        return self._loaded

    @property
    def is_detected(self) -> bool:
        """True while the equipment is paused by this plugin."""
        return self.engine.state == State.PAUSED

    @property
    def name(self) -> str:
        """Logical name of the manager."""
        return FEATURE_MANAGER_NAME

    @property
    def hass(self) -> HomeAssistant:
        """Home Assistant instance."""
        return self._hass

    # ------------------------------------------------------------------
    # InterfaceSetpointModifier contract
    # ------------------------------------------------------------------
    def adjust_regulated_temperature(self, desired: float, regulation_step: float, command_cause: str | None) -> float | None:
        """Return the setback while paused, None (keep VTherm's value) otherwise.

        A change the user made on the device itself (``underlying_tracking``)
        is left alone: the engine sees the new setpoint on the next cycle.
        """
        del regulation_step
        if self.engine.state != State.PAUSED or command_cause == COMMAND_CAUSE_UNDERLYING_TRACKING:
            return None
        mode = self._vtherm.vtherm_hvac_mode
        if mode == HVAC_HEAT:
            return min(desired, self.setback(HVAC_HEAT))
        if mode == HVAC_COOL:
            return max(desired, self.setback(HVAC_COOL))
        return None

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    @property
    def vtherm(self) -> "InterfaceThermostatRuntime":
        """The runtime thermostat bound to this manager."""
        return self._vtherm

    @property
    def vtherm_unique_id(self) -> str:
        """The unique_id of the bound VTherm."""
        return self._vtherm.unique_id

    @property
    def entry_id(self) -> str:
        """The config entry targeting this VTherm (the VTherm's id until loaded)."""
        return self._entry_id

    @property
    def decision(self) -> Decision | None:
        """The last decision."""
        return self._decision

    @property
    def has_energy_source(self) -> bool:
        """Whether a power or energy entity is configured."""
        return bool(self._conf.get(CONF_POWER_ENTITY))

    def setback(self, hvac_mode: str) -> float:
        """The setpoint sent while paused: configured, else the VTherm's own limit."""
        key, attribute, fallback = (CONF_SETBACK_HEAT, "min_temp", FALLBACK_SETBACK_HEAT) if hvac_mode == HVAC_HEAT else (CONF_SETBACK_COOL, "max_temp", FALLBACK_SETBACK_COOL)
        value = self._conf.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        state = self._hass.states.get(self._vtherm.entity_id)
        limit = state.attributes.get(attribute) if state is not None else None
        return float(limit) if isinstance(limit, (int, float)) else fallback

    async def async_set_enabled(self, enabled: bool) -> None:
        """Turn the plugin on or off for this VTherm. Off releases a pause at the next cycle."""
        self.enabled = enabled
        write_event_log(_LOGGER, self, f"smart pause {'enabled' if enabled else 'disabled'}")
        await self.async_save()

    def ensure_entities(self) -> None:
        """Create this plugin's entities once their platform callbacks are up."""
        from .sensor import build_entities as build_sensors  # pylint: disable=import-outside-toplevel
        from .switch import build_entities as build_switches  # pylint: disable=import-outside-toplevel

        registry = add_entities_registry(self._hass).get(self._vtherm.unique_id)
        if not registry or not self._loaded:
            return
        for platform, build in ((PLATFORM_SENSOR, build_sensors), (PLATFORM_SWITCH, build_switches)):
            add_entities = registry.get(platform)
            if add_entities is None:
                continue
            new_entities = build(self)
            if new_entities:
                add_entities(new_entities)
                self._entity_listeners.extend(new_entities)

    def push_to_entities(self) -> None:
        """Write the new state of every entity of this manager."""
        for entity in self._entity_listeners:
            if entity.hass is not None:
                entity.async_write_ha_state()

    def add_custom_attributes(self, extra_state_attributes: dict[str, Any]) -> None:
        """Expose the decision inside the VTherm's own state attributes.

        ``state`` is what another plugin (vtherm_mpc_fan) reads to stand down
        while the equipment is paused.
        """
        decision = self._decision
        mode = self._vtherm.vtherm_hvac_mode or HVAC_HEAT
        extra_state_attributes[ATTR_SECTION] = {
            "enabled": self.enabled,
            "state": str(self.engine.state),
            "reason": decision.reason if decision else None,
            "profile": decision.profile if decision else None,
            "autonomy_minutes": round(decision.autonomy_minutes) if decision and decision.autonomy_minutes is not None else None,
            "limit": decision.limit if decision else None,
            "setback": self.setback(mode) if mode in DRIVEN_HVAC_MODES else None,
            "pauses_today": self.engine.pauses_today,
            "heat_loss": round(self.model.heat_loss(mode if mode in DRIVEN_HVAC_MODES else HVAC_HEAT), 4),
            "solar_gain": round(self.model.solar_gain(mode if mode in DRIVEN_HVAC_MODES else HVAC_HEAT), 3),
            "model_ready": self.model.is_ready(mode) if mode in DRIVEN_HVAC_MODES else False,
            "forecast": self._forecast.available if self._forecast else False,
        }

    def __str__(self) -> str:
        return f"SmartPause-{self._name}"

    # ------------------------------------------------------------------
    # Loading and saving
    # ------------------------------------------------------------------
    def _now(self) -> float:
        """VTherm's clock as epoch seconds (tests freeze it there)."""
        now = getattr(self._vtherm, "now", None)
        return now.timestamp() if isinstance(now, datetime) else time.time()

    def _read_config(self) -> None:
        """Load the merged config of the entry targeting this VTherm."""
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_TARGET_VTHERM) == self._vtherm.unique_id:
                self._conf = merged_config(entry)
                self._entry_id = entry.entry_id
                return

    async def _async_ensure_loaded(self) -> None:
        """Read the config and the persisted model, once."""
        if self._loaded:
            return
        self._read_config()
        self.engine = DecisionEngine(settings_from_config(self._conf))
        self._forecast = WeatherForecast(self._hass, self._conf.get(CONF_WEATHER_ENTITY))
        self._store = Store(self._hass, STORAGE_VERSION, f"{STORAGE_KEY}.{self._entry_id}")
        data = await self._store.async_load() or {}
        self.model = ThermalModel.from_dict(data.get("model"))
        self.engine.restore(data.get("engine"))
        self.enabled = bool(data.get("enabled", True))
        self.avoided_energy_kwh = float(data.get("avoided_energy_kwh") or 0.0)
        self._loaded = True
        _LOGGER.info("%s - loaded (%d learned segments, state %s)", self, len(self.model.segments), self.engine.state)

    async def async_save(self) -> None:
        """Persist the model, the pause state and the counters."""
        if self._store is None:
            return
        await self._store.async_save(
            {
                "model": self.model.to_dict(),
                "engine": self.engine.to_dict(),
                "enabled": self.enabled,
                "avoided_energy_kwh": self.avoided_energy_kwh,
            }
        )

    # ------------------------------------------------------------------
    # The cycle
    # ------------------------------------------------------------------
    def _entity_value(self, key: str) -> tuple[str | None, dict]:
        """Return the state and attributes of a configured entity, or (None, {})."""
        entity_id = self._conf.get(key)
        state = self._hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None, {}
        return state.state, dict(state.attributes)

    def _is_on(self, key: str) -> bool | None:
        """Whether a configured on/off entity is on, None when absent or unknown."""
        value, _ = self._entity_value(key)
        return None if value is None else value.lower() in _ON_STATES

    def _read_activity(self, now: float) -> None:
        """Feed the activity tracker from the optional entities."""
        value, attributes = self._entity_value(CONF_POWER_ENTITY)
        power_w = energy_kwh = None
        if value is not None:
            try:
                number = float(value)
            except ValueError:
                number = None
            unit = str(attributes.get("unit_of_measurement") or "W")
            if number is not None:
                if unit == "kW":
                    power_w = number * 1000.0
                elif unit == "kWh":
                    energy_kwh = number
                elif unit == "Wh":
                    energy_kwh = number / 1000.0
                else:
                    power_w = number
        self._activity.add(now, power_w=power_w, energy_kwh=energy_kwh, running=self._is_on(CONF_COMPRESSOR_ENTITY))

    def _inactive_reason(self) -> str | None:
        """Why the engine must not decide this cycle, or None."""
        if not self.enabled:
            return "disabled"
        if self._vtherm.hvac_off_reason == HVAC_OFF_REASON_WINDOW:
            return "window"
        if self._is_on(CONF_DEFROST_ENTITY):
            return "defrost"
        if self._vtherm.is_overpowering_detected:
            return "overpowering"
        return None

    def _learn(self, now: float, room: float | None, outdoor: float | None, sun: float) -> None:
        """Record the stretches with the equipment really stopped and learn from them.

        A stop is VTherm off (not for a window) or a pause of this plugin while
        the compressor entity, when configured, confirms it is idle. Its HVAC
        mode is what the physics says: heating when outside is colder.
        """
        mode = self._vtherm.vtherm_hvac_mode
        off_reason = self._vtherm.hvac_off_reason
        stopped = (mode not in DRIVEN_HVAC_MODES and off_reason != HVAC_OFF_REASON_WINDOW) or (
            self.engine.state == State.PAUSED and self._is_on(CONF_COMPRESSOR_ENTITY) is not True
        )
        if stopped and not self._is_on(CONF_DEFROST_ENTITY):
            if self._recorder is None and room is not None and outdoor is not None:
                self._recorder = SegmentRecorder(HVAC_HEAT if outdoor < room else HVAC_COOL, now)
            if self._recorder is not None:
                self._recorder.add(now, room, outdoor, sun)
            return
        if self._recorder is not None:
            segment = self._recorder.close(self._resolution.value)
            self._recorder = None
            if segment is not None and self.model.add_segment(segment):
                _LOGGER.info("%s - learned a stop of %.1f h: k=%.4f/h", self, segment.hours, self.model.heat_loss(segment.hvac_mode))

    async def _async_run_cycle(self, now: float) -> bool:
        """Read, learn, decide, account. Returns True when the pause state changed."""
        vtherm = self._vtherm
        room = vtherm.current_temperature
        outdoor = vtherm.current_outdoor_temperature
        mode = vtherm.vtherm_hvac_mode
        self._resolution.update(room)
        if self._forecast is not None:
            await self._forecast.async_refresh(now)

        def weather_at(epoch: float) -> tuple[float | None, float]:
            if self._forecast is None:
                return outdoor, 0.0
            return self._forecast.weather_at(epoch, now, outdoor)

        if outdoor is None:
            outdoor = weather_at(now)[0]
        sun = weather_at(now)[1]
        self._learn(now, room, outdoor, sun)
        self._read_activity(now)

        found = self._recovery.update(now, room, self._resolution.value)
        if found is not None:
            self.model.add_recovery(*found)

        if self.engine.state == State.PAUSED and self._last_cycle is not None and self._pause_power_w:
            self.avoided_energy_kwh += self._pause_power_w * (now - self._last_cycle) / 3_600_000.0
        self._last_cycle = now

        driven = mode in DRIVEN_HVAC_MODES
        slope = vtherm.last_temperature_slope
        observed = slope if self.engine.state != State.PAUSED else None
        inputs = Inputs(
            now=now,
            day=dt_util.as_local(dt_util.utc_from_timestamp(now)).toordinal(),
            hvac_mode=mode,
            setpoint=vtherm.target_temperature,
            room=room,
            outdoor=outdoor,
            inactive_reason=self._inactive_reason(),
            activity=self._activity.is_active(),
            model_ready=self.model.is_ready(mode) if driven else False,
            recovery_minutes=self.model.recovery_minutes(mode) if driven else 30.0,
        )
        decision = self.engine.step(inputs, lambda m, r, limit: self.model.autonomy_minutes(m, now, r, limit, weather_at, observed))
        self._decision = decision
        _LOGGER.debug("%s - %s", self, decision)

        if decision.action == Action.PAUSE:
            self._pause_power_w = self._activity.mean_power_w()
            write_event_log(
                _LOGGER,
                self,
                f"pause ({decision.profile}): room {room} °C, setpoint {inputs.setpoint} °C, autonomy {decision.autonomy_minutes:.0f} min",
            )
        elif decision.action == Action.RESUME:
            self._pause_power_w = None
            self._recovery.arm(mode if driven else HVAC_HEAT, now, room)
            write_event_log(_LOGGER, self, f"resume ({decision.reason}): room {room} °C")

        if decision.action is not None or now - self._last_save >= SAVE_INTERVAL_S:
            self._last_save = now
            await self.async_save()
        return decision.action is not None
