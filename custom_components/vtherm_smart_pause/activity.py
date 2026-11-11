"""Whether the equipment did anything lately, from whatever entity tells.

Feeds the "something to save" condition and the avoided-energy estimate. Every
source is optional: a compressor on/off entity, a power sensor (W or kW) or an
energy counter (Wh or kWh, possibly updated only every few minutes, as cloud
integrations do). With none of them the activity is unknown and the decision
assumes it.

This module imports neither Home Assistant nor ``vtherm_api``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class ActivityTracker:
    """Rolling window of what the equipment reported."""

    window_s: float
    min_power_w: float
    _power: deque = field(default_factory=deque)  # (epoch, watts)
    _energy: deque = field(default_factory=deque)  # (epoch, kWh)
    _running: deque = field(default_factory=deque)  # (epoch, bool)

    def add(self, now: float, power_w: float | None = None, energy_kwh: float | None = None, running: bool | None = None) -> None:
        """Record one cycle's readings. None means the source is absent or unreadable."""
        for queue, value in ((self._power, power_w), (self._energy, energy_kwh), (self._running, running)):
            if value is not None:
                queue.append((now, value))
            while queue and queue[0][0] < now - self.window_s:
                queue.popleft()

    def mean_power_w(self) -> float | None:
        """Mean power over the window, from the power sensor or the energy counter."""
        if self._power:
            return sum(value for _, value in self._power) / len(self._power)
        if len(self._energy) >= 2:
            (t0, e0), (t1, e1) = self._energy[0], self._energy[-1]
            if t1 > t0 and e1 >= e0:
                return (e1 - e0) * 1000.0 / ((t1 - t0) / 3600.0)
        return None

    def is_active(self) -> bool | None:
        """True when the equipment ran within the window, None when nothing tells."""
        if self._running:
            return any(value for _, value in self._running)
        power = self.mean_power_w()
        if power is None:
            return None
        return power > self.min_power_w
