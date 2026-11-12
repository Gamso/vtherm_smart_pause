"""Learn the room's heat loss from recorded history.

At install time the model is empty and the comfort profile waits for three real
stops. Home Assistant's recorder already holds them: nights with the thermostat
off. This module turns the recorded series of one thermostat (its HVAC mode, the
room temperature, the outdoor temperature) into the same
:class:`~.thermal_model.Segment` objects the live learning produces, through the
same :class:`~.thermal_model.SegmentRecorder`, so there is a single learning path.

The recorded series are irregular (a row per change), so they are resampled on a
fixed step, each value held until the next one. A stop is the thermostat off,
the same rule as the live learning.

This module imports neither Home Assistant nor ``vtherm_api``.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable

from .thermal_model import Segment, SegmentRecorder, SensorResolution

#: Step of the resampling grid. Same order as a VTherm control cycle.
STEP_SECONDS = 300.0
HVAC_OFF = "off"
HVAC_HEAT = "heat"
HVAC_COOL = "cool"

#: A recorded series: ``(epoch seconds, value)`` sorted by time, ``None`` for an unknown value.
Series = list[tuple[float, "float | str | None"]]
#: Sun in [0, 1] at an epoch.
SunAt = Callable[[float], float]


def _value_at(times: list[float], values: list, epoch: float):
    """Return the last value at or before *epoch*, None before the first one."""
    index = bisect.bisect_right(times, epoch) - 1
    return values[index] if index >= 0 else None


def learn_segments(modes: Series, rooms: Series, outdoors: Series, start: float, end: float, sun_at: SunAt) -> list[Segment]:
    """Return the stops of the thermostat between *start* and *end* as learned segments.

    *modes* are the thermostat's HVAC mode states, *rooms* its room temperature
    and *outdoors* the outdoor temperature. A stop that is still going on at
    *end* is closed there. Stretches too short or too flat to teach anything are
    dropped by :meth:`SegmentRecorder.close`. Plausibility of ``k`` is checked
    by the model when the segment is added, not here.
    """
    mode_times, mode_values = [t for t, _ in modes], [v for _, v in modes]
    room_times, room_values = [t for t, _ in rooms], [v for _, v in rooms]
    out_times, out_values = [t for t, _ in outdoors], [v for _, v in outdoors]

    resolution = SensorResolution()
    for value in room_values:
        resolution.update(value if isinstance(value, (int, float)) else None)

    segments: list[Segment] = []
    recorder: SegmentRecorder | None = None

    def close() -> None:
        nonlocal recorder
        if recorder is not None:
            segment = recorder.close(resolution.value)
            if segment is not None:
                segments.append(segment)
            recorder = None

    epoch = start
    while epoch <= end:
        mode = _value_at(mode_times, mode_values, epoch)
        room = _value_at(room_times, room_values, epoch)
        outdoor = _value_at(out_times, out_values, epoch)
        if mode == HVAC_OFF:
            if recorder is None and isinstance(room, (int, float)) and isinstance(outdoor, (int, float)):
                recorder = SegmentRecorder(HVAC_HEAT if outdoor < room else HVAC_COOL, epoch)
            if recorder is not None:
                recorder.add(epoch, room if isinstance(room, (int, float)) else None, outdoor if isinstance(outdoor, (int, float)) else None, sun_at(epoch))
        else:
            close()
        epoch += STEP_SECONDS
    close()
    return segments
