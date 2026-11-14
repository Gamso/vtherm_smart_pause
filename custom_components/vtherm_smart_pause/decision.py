"""Pause decision: when to pause the equipment, and when to give it back.

One engine per thermostat, called once per VTherm control cycle with the
current readings. It pauses only when every condition holds -- the room has been
past the setpoint by ``excess`` for ``excess_minutes``, it will hold long enough
without the equipment, there is something to save, and the compressor guards
allow it -- and resumes as soon as one comfort condition calls for it.

Two profiles share the logic and differ in their margins: the setpoint compared
with a per-mode threshold picks one (a comfort slot is warmer than an eco one in
heating, cooler in cooling). Error and comparisons use the **user's** setpoint,
never VTherm's regulated one.

This module imports neither Home Assistant nor ``vtherm_api``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

HVAC_HEAT = "heat"
HVAC_COOL = "cool"
DRIVEN_HVAC_MODES = (HVAC_HEAT, HVAC_COOL)

PROFILE_COMFORT = "comfort"
PROFILE_ECO = "eco"

#: Tolerance on temperature comparisons: 20.0 - 20.2 is -0.19999..., and a
#: sensor reading exactly at a threshold must count as reaching it.
EPSILON = 1e-6


class State(StrEnum):
    """What the engine is doing with the equipment."""

    RUNNING = "running"  # equipment left to VTherm
    PAUSED = "paused"  # equipment paused by this plugin
    INACTIVE = "inactive"  # nothing to decide: off, window, defrost, disabled...


class Action(StrEnum):
    """What the caller must do after a step."""

    PAUSE = "pause"
    RESUME = "resume"


@dataclass(frozen=True)
class Profile:
    """Margins of one profile."""

    excess: float  # °C past the setpoint before a pause is considered
    excess_minutes: float  # how long the excess must last
    margin: float  # comfort limit: setpoint -/+ margin
    min_pause_minutes: float  # predicted autonomy needed to pause


#: Defaults, calibrated on the replay of November 2026 (docs/study.md, §1.1, and
#: tests/test_replay.py): see docs/study.md, §8.
DEFAULT_COMFORT = Profile(excess=0.3, excess_minutes=20.0, margin=0.3, min_pause_minutes=60.0)
DEFAULT_ECO = Profile(excess=0.2, excess_minutes=10.0, margin=1.0, min_pause_minutes=30.0)


@dataclass(frozen=True)
class Settings:
    """Everything configurable about the decision."""

    comfort: Profile = DEFAULT_COMFORT
    eco: Profile = DEFAULT_ECO
    #: Setpoint at or above which heating uses the comfort profile. None: always comfort.
    threshold_heat: float | None = None
    #: Setpoint at or below which cooling uses the comfort profile. None: always comfort.
    threshold_cool: float | None = None
    min_run_minutes: float = 15.0  # after a resume, before the next pause
    min_off_minutes: float = 20.0  # shortest pause, unless comfort calls
    max_pauses_per_day: int = 8
    outdoor_floor_heat: float = -5.0  # no pause in heating below this outdoor temperature
    outdoor_ceiling_cool: float = 40.0  # no pause in cooling above this one
    restart_buffer_minutes: float = 10.0  # resume this long before the predicted crossing, on top of the recovery


@dataclass(frozen=True)
class Inputs:
    """Readings of one cycle."""

    now: float  # epoch seconds
    day: int  # local calendar day (ordinal): resets the daily pause count
    hvac_mode: str | None  # VTherm's effective mode
    setpoint: float | None  # the user's setpoint
    room: float | None
    outdoor: float | None = None
    #: A reason not to decide at all (window, defrost, disabled...). None when free.
    inactive_reason: str | None = None
    recovery_minutes: float = 30.0


#: (hvac_mode, room, limit) -> predicted minutes before the room crosses limit, equipment paused.
Predictor = Callable[[str, float, float], float]


@dataclass(frozen=True)
class Decision:
    """Outcome of one step."""

    state: State
    action: Action | None
    reason: str
    profile: str | None = None
    autonomy_minutes: float | None = None
    limit: float | None = None


@dataclass
class DecisionEngine:
    """The pause state machine of one thermostat."""

    settings: Settings = field(default_factory=Settings)
    state: State = State.RUNNING
    paused_since: float | None = None
    last_resume: float | None = None
    excess_since: float | None = None
    pauses_today: int = 0
    day: int | None = None
    _last_setpoint: float | None = None
    _last_mode: str | None = None

    def profile_for(self, hvac_mode: str, setpoint: float) -> tuple[str, Profile]:
        """Return the profile the setpoint selects in this HVAC mode."""
        if hvac_mode == HVAC_HEAT:
            threshold = self.settings.threshold_heat
            comfort = threshold is None or setpoint >= threshold
        else:
            threshold = self.settings.threshold_cool
            comfort = threshold is None or setpoint <= threshold
        return (PROFILE_COMFORT, self.settings.comfort) if comfort else (PROFILE_ECO, self.settings.eco)

    def step(self, inputs: Inputs, predict: Predictor) -> Decision:
        """Advance one cycle and say whether to pause, resume or hold."""
        if self.day != inputs.day:
            self.day, self.pauses_today = inputs.day, 0

        inactive = self._inactive_reason(inputs)
        if inactive is not None:
            return self._go_inactive(inputs, inactive)
        if self.state == State.INACTIVE:
            self.state = State.RUNNING

        mode, setpoint, room = inputs.hvac_mode, inputs.setpoint, inputs.room
        assert mode is not None and setpoint is not None and room is not None  # guaranteed by _inactive_reason
        sign = 1.0 if mode == HVAC_HEAT else -1.0
        profile_name, profile = self.profile_for(mode, setpoint)
        need = sign * (setpoint - room)
        limit = setpoint - sign * profile.margin

        changed = self._last_setpoint is not None and (setpoint != self._last_setpoint or mode != self._last_mode)
        self._last_setpoint, self._last_mode = setpoint, mode
        if changed:
            self.excess_since = None
        if need <= -profile.excess + EPSILON:
            self.excess_since = self.excess_since if self.excess_since is not None else inputs.now
        else:
            self.excess_since = None

        autonomy = predict(mode, room, limit)

        def decision(action: Action | None, reason: str) -> Decision:
            return Decision(self.state, action, reason, profile_name, autonomy, limit)

        if self.state == State.PAUSED:
            reason = self._resume_reason(inputs, profile, need, autonomy, changed)
            if reason is None:
                return decision(None, "holding")
            self.state, self.paused_since, self.last_resume = State.RUNNING, None, inputs.now
            return decision(Action.RESUME, reason)

        blocker = self._pause_blocker(inputs, profile_name, profile, autonomy)
        if blocker is not None:
            return decision(None, blocker)
        self.state, self.paused_since = State.PAUSED, inputs.now
        self.pauses_today += 1
        return decision(Action.PAUSE, "excess")

    # ------------------------------------------------------------------
    def _inactive_reason(self, inputs: Inputs) -> str | None:
        """Return why nothing can be decided this cycle, or None."""
        if inputs.inactive_reason:
            return inputs.inactive_reason
        if inputs.hvac_mode not in DRIVEN_HVAC_MODES:
            return "hvac_mode"
        if inputs.setpoint is None or inputs.room is None:
            return "no_reading"
        return None

    def _go_inactive(self, inputs: Inputs, reason: str) -> Decision:
        """Release a pause and stop deciding."""
        action = Action.RESUME if self.state == State.PAUSED else None
        if action is not None:
            self.last_resume = inputs.now
        self.state, self.paused_since, self.excess_since = State.INACTIVE, None, None
        self._last_setpoint = self._last_mode = None
        return Decision(self.state, action, reason)

    def _resume_reason(self, inputs: Inputs, profile: Profile, need: float, autonomy: float, changed: bool) -> str | None:
        """Return why a pause must end now, or None to hold it."""
        if need >= profile.margin / 2.0 - EPSILON:
            return "comfort"
        if changed and need > -profile.excess + EPSILON:
            return "setpoint_change"
        paused_minutes = (inputs.now - (self.paused_since or inputs.now)) / 60.0
        if paused_minutes >= self.settings.min_off_minutes and autonomy <= inputs.recovery_minutes + self.settings.restart_buffer_minutes:
            return "autonomy"
        return None

    def _pause_blocker(self, inputs: Inputs, profile_name: str, profile: Profile, autonomy: float) -> str | None:  # pylint: disable=too-many-return-statements
        """Return the first condition that forbids a pause now, or None to pause.

        One return per condition, in a fixed order: the first blocker is the
        reason shown to the user, so the order is part of the behaviour.
        """
        s = self.settings
        if self.excess_since is None:
            return "no_excess"
        if (inputs.now - self.excess_since) / 60.0 < profile.excess_minutes:
            return "excess_too_short"
        needed = max(profile.min_pause_minutes, s.min_off_minutes + inputs.recovery_minutes + s.restart_buffer_minutes)
        if autonomy < needed:
            return "short_autonomy"
        if inputs.outdoor is not None:
            if inputs.hvac_mode == HVAC_HEAT and inputs.outdoor < s.outdoor_floor_heat:
                return "outdoor_too_cold"
            if inputs.hvac_mode == HVAC_COOL and inputs.outdoor > s.outdoor_ceiling_cool:
                return "outdoor_too_hot"
        if self.last_resume is not None and (inputs.now - self.last_resume) / 60.0 < s.min_run_minutes:
            return "min_run"
        if self.pauses_today >= s.max_pauses_per_day:
            return "max_pauses"
        return None

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """Serialise what must survive a restart."""
        return {
            "state": str(self.state),
            "paused_since": self.paused_since,
            "last_resume": self.last_resume,
            "pauses_today": self.pauses_today,
            "day": self.day,
        }

    def restore(self, data: dict | None) -> None:
        """Restore a serialised state. A pause comes back as a pause."""
        if not isinstance(data, dict):
            return
        if data.get("state") == State.PAUSED and isinstance(data.get("paused_since"), (int, float)):
            self.state, self.paused_since = State.PAUSED, float(data["paused_since"])
        if isinstance(data.get("last_resume"), (int, float)):
            self.last_resume = float(data["last_resume"])
        if isinstance(data.get("day"), int) and isinstance(data.get("pauses_today"), int):
            self.day, self.pauses_today = data["day"], data["pauses_today"]
