"""The VTherm feature manager: one per thermostat, driven by VTherm's control cycle.

VTherm calls :meth:`SmartPauseFeatureManager.refresh_state` once per control
cycle. Each cycle reads the thermostat, learns from real stops, asks the
:class:`~.decision.DecisionEngine` whether to pause or resume, and publishes the
result. The pause itself goes through the setpoint-modifier contract of
``vtherm_api``: while paused, :meth:`adjust_regulated_temperature` returns the
setback temperature, so the equipment's own thermostat goes idle while it stays
on and VTherm stays in its HVAC mode. VTherm sends the regulated temperature
before it refreshes its plugins, so on a pause or a resume this manager asks
VTherm for one forced control cycle two seconds later: the new
setpoint is sent at once, instead of a cycle later or after VTherm's minimal
auto-regulation period.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.components.climate import HVACAction
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_SECTION,
    COMMAND_CAUSE_UNDERLYING_TRACKING,
    CONF_COMFORT_EXCESS,
    CONF_COMFORT_EXCESS_MINUTES,
    CONF_COMFORT_MARGIN,
    CONF_COMFORT_MIN_PAUSE,
    CONF_ECO_EXCESS,
    CONF_ECO_EXCESS_MINUTES,
    CONF_ECO_MARGIN,
    CONF_ECO_MIN_PAUSE,
    CONF_MAX_PAUSES_PER_DAY,
    CONF_MIN_OFF_MINUTES,
    CONF_MIN_RUN_MINUTES,
    CONF_OUTDOOR_CEILING_COOL,
    CONF_OUTDOOR_FLOOR_HEAT,
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
    VTHERM_AUTO_START_STOP_SUFFIX,
    VTHERM_CONF_EXTERNAL_TEMP_SENSOR,
)
from .bootstrap import async_history_segments
from .decision import DRIVEN_HVAC_MODES, HVAC_COOL, HVAC_HEAT, PROFILE_ECO, Action, Decision, DecisionEngine, Inputs, Profile, Settings, State
from .forecast import WeatherForecast
from .history_learning import HVAC_OFF
from .log import get_logger, write_event_log
from .registry import add_entities_registry, entity_bucket, managers, merged_config
from .thermal_model import PauseEvaluator, RecoveryTracker, SegmentRecorder, SensorResolution, ThermalModel

if TYPE_CHECKING:
    from vtherm_api.interfaces import InterfaceThermostatRuntime

_LOGGER = get_logger(__name__)

#: Delay before the forced control cycle asked on a pause or a resume. A task started at once would
#: run at the first await, inside VTherm's current cycle; VTherm defers its own late sends the same way.
FORCED_REGULATION_DELAY_S = 2.0

#: Resume reasons after which the pause can be compared with the model: the room was left alone until then.
EVALUATED_RESUME_REASONS = ("comfort", "setpoint_change", "autonomy")


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
        #: Segments learned from the recorder history at install time, and whether that is still to do.
        self.history_segments = 0
        self._bootstrap_pending = False
        self._forecast: WeatherForecast | None = None
        self._resolution = SensorResolution()
        self._recovery = RecoveryTracker()
        self._evaluator = PauseEvaluator()
        self._recorder: SegmentRecorder | None = None
        #: What VTherm's auto start/stop switch was last set to (True: on), None until the first write.
        self._native_applied: bool | None = None
        self._native_missing_logged = False
        self._decision: Decision | None = None
        #: Cancels the forced control cycle scheduled on the last pause or resume, while it is pending.
        self._regulation_cancel = None
        self._regulation_running = False
        self._listeners: list = []
        self._entity_listeners: list = []

    # ------------------------------------------------------------------
    # InterfaceFeatureManager contract
    # ------------------------------------------------------------------
    def post_init(self, entry_infos: Any) -> None:
        """Publish the manager so the entity platforms can find it."""
        del entry_infos
        managers(self._hass)[self._vtherm.unique_id] = self

    async def start_listening(self, force: bool = False) -> None:
        """Load the persisted model."""
        del force
        await self._async_ensure_loaded()

    def stop_listening(self) -> bool | None:
        """Remove every listener registered through :meth:`add_listener`, and drop a pending forced cycle."""
        while self._listeners:
            self._listeners.pop()()
        if self._regulation_cancel is not None:
            self._regulation_cancel()
            self._regulation_cancel = None

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
        mode = self.hvac_mode
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
    def hvac_mode(self) -> str | None:
        """VTherm's effective HVAC mode as a plain string.

        VTherm returns its own mode object, which compares equal to a string
        but is not hashable: it must never reach a dict key or the Store.
        """
        mode = self._vtherm.vtherm_hvac_mode
        return None if mode is None else str(mode)

    @property
    def requested_hvac_mode(self) -> str | None:
        """The HVAC mode the user asked for, before feature overrides, as a plain string."""
        mode = self._vtherm.requested_hvac_mode
        return None if mode is None else str(mode)

    @property
    def entry_id(self) -> str:
        """The config entry targeting this VTherm (the VTherm's id until loaded)."""
        return self._entry_id

    @property
    def decision(self) -> Decision | None:
        """The last decision."""
        return self._decision

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
        """Create this plugin's entities once their platform callbacks are up, and take over the existing ones."""
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
        # Adopt every entity of this VTherm, the ones just built and the ones a previous
        # manager built: VTherm creates a new manager each time the thermostat reloads,
        # while the entities live as long as this plugin's config entry.
        for entities in entity_bucket(self._hass, self._vtherm.unique_id).values():
            for entity in entities:
                if entity not in self._entity_listeners:
                    entity.bind(self)
                    self._entity_listeners.append(entity)

    def push_to_entities(self) -> None:
        """Write the new state of every entity of this manager."""
        for entity in self._entity_listeners:
            if entity.hass is not None:
                entity.async_write_ha_state()

    def add_custom_attributes(self, extra_state_attributes: dict[str, Any]) -> None:
        """Expose the decision inside the VTherm's own state attributes.

        ``state`` is what another plugin (vtherm_mpc_fan) reads to stand down
        while the equipment is paused. VTherm excludes only its own managers'
        attributes from the recorder, so this section is recorded: it carries
        nothing that changes on every cycle (the autonomy has its own sensor).
        """
        decision = self._decision
        mode = self.hvac_mode or HVAC_HEAT
        extra_state_attributes[ATTR_SECTION] = {
            "enabled": self.enabled,
            "state": str(self.engine.state),
            "reason": decision.reason if decision else None,
            "profile": decision.profile if decision else None,
            "limit": decision.limit if decision else None,
            "setback": self.setback(mode) if mode in DRIVEN_HVAC_MODES else None,
            "pauses_today": self.engine.pauses_today,
            "vtherm_auto_start_stop": self._native_applied,
            "history_segments": self.history_segments,
            "heat_loss": round(self.model.heat_loss(mode if mode in DRIVEN_HVAC_MODES else HVAC_HEAT), 4),
            "solar_gain": round(self.model.solar_gain(mode if mode in DRIVEN_HVAC_MODES else HVAC_HEAT), 3),
            "model_ready": self.model.is_ready(mode) if mode in DRIVEN_HVAC_MODES else False,
            "model_error": self._rounded(self.model.prediction_error(mode if mode in DRIVEN_HVAC_MODES else HVAC_HEAT)),
            "forecast": self._forecast.available if self._forecast else False,
        }

    @staticmethod
    def _rounded(value: float | None, digits: int = 2) -> float | None:
        """Round a value that may not be known yet."""
        return None if value is None else round(value, digits)

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
        self.history_segments = int(data.get("history_segments") or 0)
        self._bootstrap_pending = not self.model.segments
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
                "history_segments": self.history_segments,
            }
        )

    # ------------------------------------------------------------------
    # The cycle
    # ------------------------------------------------------------------
    def _native_switch(self) -> str | None:
        """Entity id of VTherm's own auto start/stop switch, None when that feature is not configured.

        VTherm creates it under the thermostat's config entry, whose entry_id is
        the thermostat's unique_id, with a unique_id ending in ``_enable_auto_start_stop``.
        """
        registry = er.async_get(self._hass)
        for entry in er.async_entries_for_config_entry(registry, self._vtherm.unique_id):
            if entry.domain == "switch" and entry.unique_id.endswith(VTHERM_AUTO_START_STOP_SUFFIX):
                return entry.entity_id
        return None

    def _eco_side(self) -> bool:
        """Whether the user's setpoint is on the eco side of the threshold.

        Read on the requested HVAC mode: once VTherm's auto start/stop has
        switched the equipment off, the effective mode is ``off``.
        """
        mode = self.requested_hvac_mode
        if mode not in DRIVEN_HVAC_MODES:
            mode = self.hvac_mode
        setpoint = self._vtherm.target_temperature
        if mode not in DRIVEN_HVAC_MODES or setpoint is None:
            return False
        return self.engine.profile_for(mode, setpoint)[0] == PROFILE_ECO

    async def _async_drive_native(self, switch: str, delegated: bool) -> None:
        """Turn VTherm's auto start/stop on when it decides, off when this plugin does.

        Only one of them may decide. Written on transitions only, as HomeShift
        did: a flip by hand holds until the next transition.
        """
        if delegated == self._native_applied:
            return
        state = self._hass.states.get(switch)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        self._native_applied = delegated
        if (state.state == STATE_ON) == delegated:
            return
        write_event_log(_LOGGER, self, f"VTherm auto start/stop {'enabled (eco, delegated)' if delegated else 'disabled (smart pause decides)'}")
        # Not blocking: the switch updates this very VTherm, whose control cycle is running.
        await self._hass.services.async_call("switch", "turn_on" if delegated else "turn_off", {"entity_id": switch}, blocking=False)

    async def _async_delegation(self) -> bool:
        """Drive VTherm's auto start/stop and return whether it decides this cycle.

        It decides on the eco side of the mode's threshold, a mode without a
        threshold never delegates. Without the switch (the feature is not
        configured on the thermostat), the smart pause decides on both sides,
        in its eco profile below the threshold.
        """
        if not self.enabled:
            return False
        switch = self._native_switch()
        wanted = self._eco_side()
        if switch is None:
            if wanted and not self._native_missing_logged:
                self._native_missing_logged = True
                _LOGGER.warning("%s - a threshold is set, but VTherm's auto start/stop is not configured on this thermostat: smart pause decides", self)
            return False
        await self._async_drive_native(switch, wanted)
        return wanted

    def _inactive_reason(self, delegated: bool = False) -> str | None:
        """Why the engine must not decide this cycle, or None."""
        if not self.enabled:
            return "disabled"
        if delegated:
            return "delegated"
        if self._vtherm.hvac_off_reason == HVAC_OFF_REASON_WINDOW:
            return "window"
        if self._vtherm.hvac_action == HVACAction.DEFROSTING:
            return "defrost"
        if self._vtherm.is_overpowering_detected:
            return "overpowering"
        return None

    def _learn(self, now: float, room: float | None, outdoor: float | None, sun: float) -> None:
        """Record the stretches with the equipment really stopped and learn from them.

        A stop is VTherm off (not fan only or dry, nor for a window, nor while it
        defrosts) or a pause of this plugin. Its HVAC mode is what the physics says: heating when
        outside is colder.
        """
        mode = self.hvac_mode
        off_reason = self._vtherm.hvac_off_reason
        stopped = (mode == HVAC_OFF and off_reason != HVAC_OFF_REASON_WINDOW) or self.engine.state == State.PAUSED
        if stopped and self._vtherm.hvac_action != HVACAction.DEFROSTING:
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

    def _evaluate_pause(self, now: float, room: float | None, reason: str) -> None:
        """Compare the pause that just ended with what the model predicted for it.

        Only a pause the room went through undisturbed is compared: one ended
        by a window, a mode change or the switch says nothing about the model.
        """
        if reason not in EVALUATED_RESUME_REASONS:
            self._evaluator.cancel()
            return
        predicted = self._evaluator.predicted
        found = self._evaluator.close(now, room)
        if found is None:
            return
        self.model.add_error(*found)
        write_event_log(_LOGGER, self, f"model check: predicted {predicted:.2f} °C, measured {room} °C, error {found[1]:+.2f} °C")

    async def _async_bootstrap(self, now: float) -> None:
        """Learn the thermostat's past stops from the recorder history, once, while the model is empty.

        Read on the first cycle rather than at load, when the recorder may not
        be up yet. A failure is logged and never retried until the next restart:
        the live learning goes on without it.
        """
        if not self._bootstrap_pending:
            return
        self._bootstrap_pending = False
        entry_infos = self._vtherm.entry_infos
        outdoor_entity = entry_infos.get(VTHERM_CONF_EXTERNAL_TEMP_SENSOR) if hasattr(entry_infos, "get") else None
        try:
            segments = await async_history_segments(self._hass, self._vtherm.entity_id, outdoor_entity, self._conf.get(CONF_WEATHER_ENTITY), now)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning("%s - could not read the history, learning from scratch", self, exc_info=True)
            return
        added = sum(1 for segment in segments if self.model.add_segment(segment))
        self.history_segments = added
        write_event_log(_LOGGER, self, f"learned {added} stops from the recorder history, k={self.model.heat_loss(HVAC_HEAT):.4f}/h")
        if added:
            await self.async_save()

    async def _async_run_cycle(self, now: float) -> bool:
        """Read, learn, decide, account. Returns True when the pause state changed."""
        await self._async_bootstrap(now)
        vtherm = self._vtherm
        room = vtherm.current_temperature
        outdoor = vtherm.current_outdoor_temperature
        mode = self.hvac_mode
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

        found = self._recovery.update(now, room, self._resolution.value)
        if found is not None:
            self.model.add_recovery(*found)

        # Before the decision: on the cycle that resumes, the prediction must reach *now*.
        self._evaluator.step(self.model, now, outdoor, sun)

        delegated = await self._async_delegation()
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
            inactive_reason=self._inactive_reason(delegated),
            model_ready=self.model.is_ready(mode) if driven else False,
            recovery_minutes=self.model.recovery_minutes(mode) if driven else 30.0,
        )
        decision = self.engine.step(inputs, lambda m, r, limit: self.model.autonomy_minutes(m, now, r, limit, weather_at, observed))
        self._decision = decision
        _LOGGER.debug("%s - %s", self, decision)

        if decision.action == Action.PAUSE:
            self._evaluator.arm(mode, now, room)
            write_event_log(
                _LOGGER,
                self,
                f"pause ({decision.profile}): room {room} °C, setpoint {inputs.setpoint} °C, autonomy {decision.autonomy_minutes:.0f} min",
            )
        elif decision.action == Action.RESUME:
            self._recovery.arm(mode if driven else HVAC_HEAT, now, room)
            write_event_log(_LOGGER, self, f"resume ({decision.reason}): room {room} °C")
            self._evaluate_pause(now, room, decision.reason)

        if decision.action is not None:
            self._request_regulation()
        if decision.action is not None or now - self._last_save >= SAVE_INTERVAL_S:
            self._last_save = now
            await self.async_save()
        return decision.action is not None

    def _request_regulation(self) -> None:
        """Ask VTherm for one forced control cycle, a moment after the current one.

        VTherm sends the regulated temperature before it refreshes its plugins:
        without this, a pause or a resume decided now would reach the equipment
        a cycle later, or once VTherm's minimal auto-regulation period has
        elapsed. Asked on transitions only, which the decision engine's guards
        bound (minimum run, minimum pause, pauses per day): it changes when the
        setpoint is sent, not how often the equipment stops.
        """
        if self._regulation_cancel is not None or self._regulation_running:
            return
        if not callable(getattr(self._vtherm, "async_control_heating", None)):
            return
        self._regulation_cancel = async_call_later(self._hass, FORCED_REGULATION_DELAY_S, self._async_force_regulation)

    async def _async_force_regulation(self, _now: datetime) -> None:
        """Run VTherm's forced control cycle. A failure is logged: the next regular cycle sends the setpoint anyway."""
        self._regulation_cancel = None
        self._regulation_running = True
        try:
            await self._vtherm.async_control_heating(force=True)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning("%s - the forced regulation failed, the setpoint goes with the next cycle", self, exc_info=True)
        finally:
            self._regulation_running = False
