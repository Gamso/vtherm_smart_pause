"""Learn the room's heat loss from recorded history.

At install time the model is empty and the comfort profile waits for three real
stops. Home Assistant's recorder already holds them: nights with the thermostat
off. This module turns the recorded series of one thermostat (its HVAC mode, the
room temperature, the outdoor temperature) into the same
:class:`~.thermal_model.Segment` objects the live learning produces, through the
same :class:`~.thermal_model.SegmentRecorder`, so there is a single learning path.

The recorded series are irregular (a row per change), so they are resampled on a
fixed step, each value held until the next one. A stop is the thermostat off,
the same rule as the live learning. The recorder does not keep why it was off:
when the thermostat has a window sensor, its history tells the stops caused by
an open window apart, and those are dropped.

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
#: State of a window sensor (binary sensor, input boolean...) while the window is open.
WINDOW_OPEN = "on"

#: A recorded series: ``(epoch seconds, value)`` sorted by time, ``None`` for an unknown value.
Series = list[tuple[float, "float | str | None"]]
#: Sun in [0, 1] at an epoch.
SunAt = Callable[[float], float]


def _value_at(times: list[float], values: list, epoch: float):
    """Return the last value at or before *epoch*, None before the first one."""
    index = bisect.bisect_right(times, epoch) - 1
    return values[index] if index >= 0 else None


def learn_segments(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    modes: Series,
    rooms: Series,
    outdoors: Series,
    start: float,
    end: float,
    sun_at: SunAt,
    windows: Series | None = None,
) -> list[Segment]:
    """Return the stops of the thermostat between *start* and *end* as learned segments.

    *modes* are the thermostat's HVAC mode states, *rooms* its room temperature
    and *outdoors* the outdoor temperature. A stop that is still going on at
    *end* is closed there. Stretches too short or too flat to teach anything are
    dropped by :meth:`SegmentRecorder.close`. Plausibility of ``k`` is checked
    by the model when the segment is added, not here.

    *windows* is the window sensor's state. A stop during which the window was
    open at any time is dropped whole: the room does not cool naturally, nor
    while it warms back up once the window is closed.
    """
    mode_times, mode_values = [t for t, _ in modes], [v for _, v in modes]
    room_times, room_values = [t for t, _ in rooms], [v for _, v in rooms]
    out_times, out_values = [t for t, _ in outdoors], [v for _, v in outdoors]
    window_times, window_values = [t for t, _ in windows or []], [v for _, v in windows or []]

    resolution = SensorResolution()
    for value in room_values:
        resolution.update(value if isinstance(value, (int, float)) else None)

    segments: list[Segment] = []
    recorder: SegmentRecorder | None = None
    #: The current stop saw the window open: nothing of it is learned.
    spoiled = False

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
        if mode == HVAC_OFF and (spoiled or _value_at(window_times, window_values, epoch) == WINDOW_OPEN):
            spoiled, recorder = True, None
        elif mode == HVAC_OFF:
            if recorder is None and isinstance(room, (int, float)) and isinstance(outdoor, (int, float)):
                recorder = SegmentRecorder(HVAC_HEAT if outdoor < room else HVAC_COOL, epoch)
            if recorder is not None:
                recorder.add(epoch, room if isinstance(room, (int, float)) else None, outdoor if isinstance(outdoor, (int, float)) else None, sun_at(epoch))
        else:
            close()
            spoiled = False
        epoch += STEP_SECONDS
    close()
    return segments
