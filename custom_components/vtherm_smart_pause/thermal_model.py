"""Learned behaviour of the room while the equipment is stopped.

The model is first-order: with the equipment off the room drifts toward the
outdoor temperature and the sun pushes it up::

    dT/dt = k * (T_out - T_in) + g * S

``k`` (1/h) is the heat-loss coefficient, ``g`` (°C/h) the solar gain at full
sun and ``S`` in [0, 1] the sun the forecast expects (clear-sky elevation
weighted by cloud cover). The same equation holds in heating (the room cools
toward a colder outside) and in cooling (it warms toward a hotter one), so one
model serves both modes. Each HVAC mode still keeps its own segments, since
windows, blinds and internal gains differ between seasons.

It learns from **segments**: stretches where the equipment was really stopped
(VTherm off, or paused by this plugin), without a window open or a defrost.
Each segment is reduced to three integrals -- the temperature change, the
exposure ``∫(T_out - T_in) dt`` and the sun ``∫S dt`` -- so the fit is a ratio
per segment and a weighted median over segments: robust to the 0.2 °C steps of
common room sensors, with no numerical library.

This module imports neither Home Assistant nor ``vtherm_api``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from statistics import median

MODEL_FORMAT = 1

#: Prudent heat-loss coefficient (1/h) used until segments are learned: a poorly
#: insulated room. Overestimating the loss shortens the predicted autonomy, so a
#: cold start pauses less, never longer.
DEFAULT_K = 0.08
#: Minutes dropped at the start of a segment: the unit's residual heat (or cold)
#: still reaches the room right after it stops.
SETTLE_MINUTES = 30.0
#: Minimum duration of a segment, settle time excluded.
MIN_SEGMENT_HOURS = 2.0
#: A segment whose exposure is smaller than this (°C·h) says nothing about k:
#: the room was too close to the outdoor temperature.
MIN_EXPOSURE = 2.0
#: A segment counts as "sunless" (it measures k alone) when its mean sun is below this.
SUNLESS_MEAN_SUN = 0.05
#: A segment measures g only when its mean sun reaches this.
SUNNY_MEAN_SUN = 0.15
#: Sunless segments needed before the model is trusted for the comfort profile.
MIN_READY_SEGMENTS = 3
#: Sunny segments needed before g is estimated (0 until then).
MIN_SUN_SEGMENTS = 2
#: Segment retention: everything from the last RETENTION_DAYS, and in any case
#: the KEEP_LATEST newest (a summer of cooling must survive the winter).
RETENTION_DAYS = 30
KEEP_LATEST = 20
#: Plausible range for one segment's k. Outside it the segment is noise (a
#: window opened unnoticed, another heat source).
K_MIN = 0.005
K_MAX = 0.5
#: Prediction horizon and step.
HORIZON_MINUTES = 360.0
STEP_MINUTES = 5.0
#: Recovery (time for the room to turn back once the equipment restarts).
DEFAULT_RECOVERY_MINUTES = 30.0
RECOVERY_GIVE_UP_MINUTES = 180.0
MAX_RECOVERIES = 20

#: (epoch seconds) -> (outdoor temperature, sun in [0, 1]). None outdoor means unknown.
WeatherAt = Callable[[float], tuple[float | None, float]]


@dataclass
class Segment:
    """One learned stretch with the equipment stopped, reduced to its integrals."""

    hvac_mode: str
    end: float  # epoch seconds
    hours: float
    rise: float  # T_in(end) - T_in(start), °C
    exposure: float  # ∫(T_out - T_in) dt, °C·h
    sun: float  # ∫S dt, h

    @property
    def mean_sun(self) -> float:
        """Average sun over the segment."""
        return self.sun / self.hours if self.hours > 0 else 0.0


@dataclass
class SegmentRecorder:
    """Accumulate the readings of one stop and turn them into a :class:`Segment`."""

    hvac_mode: str
    start: float
    readings: list[tuple[float, float, float, float]] = field(default_factory=list)

    def add(self, now: float, room: float | None, outdoor: float | None, sun: float) -> None:
        """Record one reading once the settle time has passed. Incomplete readings are skipped."""
        if room is None or outdoor is None:
            return
        if (now - self.start) / 60.0 < SETTLE_MINUTES:
            return
        self.readings.append((now, room, outdoor, max(0.0, min(1.0, sun))))

    def close(self, resolution: float) -> Segment | None:
        """Return the segment, or None when it is too short or too flat to learn from.

        A segment shorter than ``MIN_SEGMENT_HOURS`` is dropped. One that moved
        less than three sensor steps is kept only from three hours on: a flat
        room over a long stop is information (sun and losses balanced), a flat
        room over two hours may just be the sensor's quantization.
        """
        if len(self.readings) < 2:
            return None
        hours = (self.readings[-1][0] - self.readings[0][0]) / 3600.0
        if hours < MIN_SEGMENT_HOURS:
            return None
        rise = self.readings[-1][1] - self.readings[0][1]
        if abs(rise) < 3 * resolution and hours < 3.0:
            return None
        exposure = 0.0
        sun = 0.0
        for (t0, in0, out0, s0), (t1, in1, out1, s1) in zip(self.readings, self.readings[1:]):
            dt = (t1 - t0) / 3600.0
            exposure += dt * ((out0 - in0) + (out1 - in1)) / 2.0
            sun += dt * (s0 + s1) / 2.0
        if abs(exposure) < MIN_EXPOSURE:
            return None
        return Segment(self.hvac_mode, self.readings[-1][0], hours, rise, exposure, sun)


@dataclass
class SensorResolution:
    """Smallest step the room sensor reports, detected from its readings."""

    step: float | None = None
    _last: float | None = None

    def update(self, value: float | None) -> None:
        """Feed one reading."""
        if value is None:
            return
        if self._last is not None:
            delta = round(abs(value - self._last), 3)
            if delta >= 0.01 and (self.step is None or delta < self.step):
                self.step = delta
        self._last = value

    @property
    def value(self) -> float:
        """The detected step, 0.1 °C until two different readings were seen."""
        return self.step if self.step is not None else 0.1


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    """Return the weighted median of ``(value, weight)`` pairs (non-empty)."""
    ordered = sorted(pairs)
    total = sum(weight for _, weight in ordered)
    acc = 0.0
    for value, weight in ordered:
        acc += weight
        if acc >= total / 2.0:
            return value
    return ordered[-1][0]


class ThermalModel:
    """Heat-loss and solar-gain model per HVAC mode, plus the recovery delay."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.segments: list[Segment] = []
        #: Minutes the room took to turn back after a restart, per HVAC mode.
        self.recoveries: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------
    def add_segment(self, segment: Segment) -> bool:
        """Store a segment if its k is plausible. Return whether it was kept."""
        if segment.mean_sun < SUNLESS_MEAN_SUN:
            k = segment.rise / segment.exposure
            if not K_MIN <= k <= K_MAX:
                return False
        self.segments.append(segment)
        self._prune()
        return True

    def add_recovery(self, hvac_mode: str, minutes: float) -> None:
        """Store how long the room took to turn back after a restart."""
        values = self.recoveries.setdefault(hvac_mode, [])
        values.append(minutes)
        del values[:-MAX_RECOVERIES]

    def _prune(self) -> None:
        """Keep the recent segments, and the newest ones whatever their age."""
        cutoff = self._clock() - RETENTION_DAYS * 86400.0
        newest = sorted(self.segments, key=lambda s: s.end)[-KEEP_LATEST:]
        keep = {id(s) for s in newest}
        self.segments = [s for s in self.segments if s.end >= cutoff or id(s) in keep]

    def _mode_segments(self, hvac_mode: str) -> list[Segment]:
        """Segments of one HVAC mode, or every segment when that mode has none."""
        own = [s for s in self.segments if s.hvac_mode == hvac_mode]
        return own or list(self.segments)

    def sunless_count(self, hvac_mode: str) -> int:
        """Number of sunless segments learned in this HVAC mode."""
        return sum(1 for s in self.segments if s.hvac_mode == hvac_mode and s.mean_sun < SUNLESS_MEAN_SUN)

    def is_ready(self, hvac_mode: str) -> bool:
        """Whether k is learned in this mode well enough for the comfort profile."""
        return self.sunless_count(hvac_mode) >= MIN_READY_SEGMENTS

    def heat_loss(self, hvac_mode: str) -> float:
        """Return k (1/h): weighted median over sunless segments, DEFAULT_K without any."""
        pairs = [(s.rise / s.exposure, s.hours) for s in self._mode_segments(hvac_mode) if s.mean_sun < SUNLESS_MEAN_SUN]
        return _weighted_median(pairs) if pairs else DEFAULT_K

    def solar_gain(self, hvac_mode: str) -> float:
        """Return g (°C/h at full sun), 0 until enough sunny segments exist. Never negative."""
        k = self.heat_loss(hvac_mode)
        pairs = [((s.rise - k * s.exposure) / s.sun, s.hours) for s in self._mode_segments(hvac_mode) if s.mean_sun >= SUNNY_MEAN_SUN]
        if len(pairs) < MIN_SUN_SEGMENTS:
            return 0.0
        return max(0.0, _weighted_median(pairs))

    def recovery_minutes(self, hvac_mode: str) -> float:
        """Median time the room takes to turn back after a restart."""
        values = self.recoveries.get(hvac_mode) or []
        return median(values) if values else DEFAULT_RECOVERY_MINUTES

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------
    def autonomy_minutes(
        self,
        hvac_mode: str,
        now: float,
        room: float,
        limit: float,
        weather_at: WeatherAt,
        observed_slope: float | None = None,
    ) -> float:
        """Minutes until the room, equipment stopped, crosses *limit* (capped at the horizon).

        *limit* is the comfort floor in heating and the comfort ceiling in
        cooling. The trajectory integrates the model on the weather forecast.
        Without an outdoor temperature at all the room is assumed to hold (no
        exposure), so the observed slope alone bounds it.

        *observed_slope* (°C/h) is the slope measured while the equipment still
        runs. When it already heads toward the limit, the room would get there
        at least as fast once stopped, so it bounds the result.
        """
        sign = 1.0 if hvac_mode == "heat" else -1.0
        if sign * (room - limit) <= 0:
            return 0.0
        k = self.heat_loss(hvac_mode)
        g = self.solar_gain(hvac_mode)
        temp = room
        minutes = 0.0
        last_outdoor: float | None = None
        while minutes < HORIZON_MINUTES:
            outdoor, sun = weather_at(now + minutes * 60.0)
            if outdoor is None:
                outdoor = last_outdoor
            last_outdoor = outdoor
            drift = (k * (outdoor - temp) if outdoor is not None else 0.0) + g * sun
            temp += drift * STEP_MINUTES / 60.0
            minutes += STEP_MINUTES
            if sign * (temp - limit) <= 0:
                break
        if observed_slope is not None and sign * observed_slope < 0:
            minutes = min(minutes, 60.0 * sign * (room - limit) / (-sign * observed_slope))
        return min(minutes, HORIZON_MINUTES)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """Serialise the model for Home Assistant's Store."""
        return {
            "format": MODEL_FORMAT,
            "segments": [asdict(s) for s in self.segments],
            "recoveries": {mode: list(values) for mode, values in self.recoveries.items()},
        }

    @classmethod
    def from_dict(cls, data: dict | None, clock: Callable[[], float] = time.time) -> "ThermalModel":
        """Rebuild a model. Unreadable entries are dropped rather than failing the load."""
        model = cls(clock=clock)
        if not isinstance(data, dict):
            return model
        for raw in data.get("segments") or []:
            try:
                model.segments.append(Segment(**raw))
            except TypeError:
                continue
        for mode, values in (data.get("recoveries") or {}).items():
            model.recoveries[str(mode)] = [float(v) for v in values if isinstance(v, (int, float))][-MAX_RECOVERIES:]
        return model


@dataclass
class RecoveryTracker:
    """Measure how long the room keeps drifting after a restart.

    The room goes on cooling (heating) for a while once the equipment restarts:
    the unit has to warm up first. The recovery is the time from the restart to
    the worst reading, confirmed once the room has come back one sensor step
    from it. That is the lead the decision needs to restart before the floor.
    """

    hvac_mode: str | None = None
    start: float = 0.0
    worst: float = 0.0
    worst_at: float = 0.0

    def arm(self, hvac_mode: str, now: float, room: float | None) -> None:
        """Start measuring from a restart."""
        if room is None:
            self.hvac_mode = None
            return
        self.hvac_mode, self.start, self.worst, self.worst_at = hvac_mode, now, room, now

    def update(self, now: float, room: float | None, resolution: float) -> tuple[str, float] | None:
        """Return ``(hvac_mode, minutes)`` once the room has turned back toward comfort."""
        if self.hvac_mode is None or room is None:
            return None
        if (now - self.start) / 60.0 > RECOVERY_GIVE_UP_MINUTES:
            self.hvac_mode = None
            return None
        sign = 1.0 if self.hvac_mode == "heat" else -1.0
        if sign * (room - self.worst) < 0:
            self.worst, self.worst_at = room, now
            return None
        if sign * (room - self.worst) >= resolution - 1e-6:
            mode, self.hvac_mode = self.hvac_mode, None
            return mode, (self.worst_at - self.start) / 60.0
        return None
