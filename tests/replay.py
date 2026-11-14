"""Open-loop replay of recorded history through the decision engine and the thermal model.

The dataset (``tests/data/replay_*.csv``, one row per minute)
is real: the equipment ran as it did that day. Once the engine pauses, the room
it sees is no longer the recorded one -- the recorded room still had the unit's
heat -- but the model's prediction from the pause onward, on the recorded
outdoor temperature. That counterfactual makes a pause end when the model says
the room would have reached its limit, instead of lasting as long as the real,
still-heated room stayed warm.

The energy reported is the *gross* energy the equipment drew while paused: an
upper bound of the saving, since part of that heat must be supplied again after
the pause.

The thermal model learns online, as in Home Assistant: only from the nights the
thermostat was really off, never from its own simulated pauses.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from custom_components.vtherm_smart_pause.decision import Action, DecisionEngine, Inputs, Settings, State
from custom_components.vtherm_smart_pause.solar import sun_factor
from custom_components.vtherm_smart_pause.thermal_model import RecoveryTracker, SegmentRecorder, SensorResolution, ThermalModel

DATA_DIR = Path(__file__).parent / "data"
REFERENCE_DATASET = DATA_DIR / "replay_2026-11-01_2026-11-11.csv"

#: A generic location (central France), not the one the data was recorded at:
#: only the shape of the day matters to the sun proxy.
LATITUDE = 46.5
LONGITUDE = 2.5
#: Offset of the local time the dataset was recorded in (CET), for the daily pause count.
LOCAL_OFFSET_S = 3600


def _num(value: str) -> float | None:
    """Parse a numeric cell, None when empty."""
    return float(value) if value not in ("", None) else None


def load_rows(path: Path = REFERENCE_DATASET) -> list[dict]:
    """Load the replay CSV into dicts with parsed values."""
    rows = []
    with path.open(encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            rows.append(
                {
                    "t": datetime.fromisoformat(raw["time_utc"].replace("Z", "+00:00")).timestamp(),
                    "power": _num(raw["power_w"]) or 0.0,
                    "mode": raw["vtherm_hvac_mode"] or None,
                    "room": _num(raw["room_temp"]),
                    "setpoint": _num(raw["user_setpoint"]),
                    "outdoor": _num(raw["outdoor_temp"]),
                }
            )
    return rows


@dataclass
class ReplayResult:
    """What a replay measured."""

    paused_energy_kwh: float = 0.0
    total_energy_kwh: float = 0.0
    pauses: int = 0
    paused_minutes: int = 0
    max_pauses_per_day: int = 0
    #: Lowest (heating) simulated room temperature during a pause, minus the comfort limit then in force.
    worst_limit_margin: float = float("inf")
    heat_loss: float = 0.0
    sunless_segments: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


def run_replay(settings: Settings | None = None, rows: list[dict] | None = None) -> ReplayResult:
    """Replay the dataset minute by minute and return the measures."""
    rows = rows if rows is not None else load_rows()
    model = ThermalModel(clock=lambda: rows[-1]["t"])
    engine = DecisionEngine(settings or Settings())
    resolution = SensorResolution()
    recovery = RecoveryTracker()
    result = ReplayResult()
    recorder: SegmentRecorder | None = None
    pauses_by_day: dict[int, int] = {}
    sim_room: float | None = None
    pause_mode: str | None = None
    rows_by_minute = {int(r["t"] // 60): r for r in rows}

    def weather_at(epoch: float) -> tuple[float | None, float]:
        """The recorded outdoor temperature (a perfect forecast) and the sun proxy."""
        row = rows_by_minute.get(int(epoch // 60))
        outdoor = row["outdoor"] if row else None
        return outdoor, sun_factor(LATITUDE, LONGITUDE, epoch, None)

    for index, row in enumerate(rows):
        now = row["t"]
        resolution.update(row["room"])
        result.total_energy_kwh += row["power"] / 60000.0

        # Online learning from the real off periods.
        if row["mode"] == "off":
            if recorder is None:
                recorder = SegmentRecorder("heat", now)
            recorder.add(now, row["room"], row["outdoor"], weather_at(now)[1])
        elif recorder is not None:
            segment = recorder.close(resolution.value)
            if segment is not None:
                model.add_segment(segment)
            recorder = None

        # During a pause, the room follows the model, not the recorded (still heated) one.
        room = row["room"]
        if engine.state == State.PAUSED and sim_room is not None and row["outdoor"] is not None:
            k, g = model.heat_loss(pause_mode or "heat"), model.solar_gain(pause_mode or "heat")
            sim_room += (k * (row["outdoor"] - sim_room) + g * weather_at(now)[1]) / 60.0
            room = sim_room

        found = recovery.update(now, row["room"], resolution.value)
        if found is not None:
            model.add_recovery(*found)

        mode = row["mode"]
        inputs = Inputs(
            now=now,
            day=int((now + LOCAL_OFFSET_S) // 86400),
            hvac_mode=mode,
            setpoint=row["setpoint"],
            room=room,
            outdoor=row["outdoor"],
            recovery_minutes=model.recovery_minutes(mode) if mode else 30.0,
        )
        decision = engine.step(inputs, lambda m, r, lim, now=now: model.autonomy_minutes(m, now, r, lim, weather_at))
        result.reasons[decision.reason] = result.reasons.get(decision.reason, 0) + 1

        if decision.action == Action.PAUSE:
            result.pauses += 1
            pauses_by_day[inputs.day] = pauses_by_day.get(inputs.day, 0) + 1
            sim_room, pause_mode = row["room"], mode
        elif decision.action == Action.RESUME:
            sim_room = None
            recovery.arm(mode or "heat", now, row["room"])

        if engine.state == State.PAUSED:
            result.paused_minutes += 1
            result.paused_energy_kwh += row["power"] / 60000.0
            if decision.limit is not None and room is not None:
                sign = 1.0 if mode == "heat" else -1.0
                result.worst_limit_margin = min(result.worst_limit_margin, sign * (room - decision.limit))

    result.max_pauses_per_day = max(pauses_by_day.values(), default=0)
    result.heat_loss = model.heat_loss("heat")
    result.sunless_segments = model.sunless_count("heat")
    return result
