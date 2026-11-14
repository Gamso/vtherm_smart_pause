"""Tests of the thermal model, the segment recorder and the solar proxy."""

import math

from custom_components.vtherm_smart_pause.solar import sun_elevation, sun_factor
from custom_components.vtherm_smart_pause.thermal_model import (
    DEFAULT_K,
    PauseEvaluator,
    RecoveryTracker,
    Segment,
    SegmentRecorder,
    SensorResolution,
    ThermalModel,
)

START = 1_700_000_000.0


def _quantize(value: float, step: float = 0.2) -> float:
    """Round like a room sensor with *step* resolution."""
    return round(round(value / step) * step, 2)


def _record(k: float, g: float, outdoor: float, room: float, hours: float, sun=lambda _t: 0.0, mode: str = "heat") -> Segment | None:
    """Simulate a stop with known k and g, quantized to 0.2 °C, and close it."""
    recorder = SegmentRecorder(mode, START)
    temp = room
    for minute in range(int(hours * 60)):
        now = START + minute * 60.0
        s = sun(now)
        recorder.add(now, _quantize(temp), outdoor, s)
        temp += (k * (outdoor - temp) + g * s) / 60.0
    return recorder.close(0.2)


def test_recovers_k_from_quantized_nights():
    """Three quantized nights give back k within 15 %."""
    model = ThermalModel(clock=lambda: START)
    for outdoor in (2.0, 6.0, 10.0):
        segment = _record(0.045, 0.0, outdoor, 20.0, 7.0)
        assert segment is not None
        assert model.add_segment(segment)
    assert model.is_ready("heat")
    assert abs(model.heat_loss("heat") - 0.045) < 0.045 * 0.15


def test_recovers_g_from_sunny_days():
    """Sunny segments give g once k is known."""
    model = ThermalModel(clock=lambda: START)
    for outdoor in (2.0, 6.0, 10.0):
        model.add_segment(_record(0.045, 0.0, outdoor, 20.0, 7.0))

    def sun(now: float) -> float:
        """A smooth 8-hour day."""
        return max(0.0, math.sin(math.pi * (now - START) / (8 * 3600.0)))

    for outdoor in (8.0, 10.0, 12.0):
        assert model.add_segment(_record(0.045, 0.6, outdoor, 20.0, 8.0, sun=sun))
    assert abs(model.solar_gain("heat") - 0.6) < 0.25


def test_default_k_and_no_sun_without_data():
    """A fresh model is prudent: DEFAULT_K, no solar gain, not ready."""
    model = ThermalModel()
    assert model.heat_loss("heat") == DEFAULT_K
    assert model.solar_gain("heat") == 0.0
    assert not model.is_ready("heat")


def test_short_or_flat_segments_are_dropped():
    """Under two hours, or flat over less than three hours, a stop teaches nothing."""
    assert _record(0.045, 0.0, 5.0, 20.0, 1.5) is None
    assert _record(0.0, 0.0, 19.0, 20.0, 2.6) is None


def test_implausible_k_is_rejected():
    """A sunless segment whose k is out of range is not stored."""
    model = ThermalModel()
    assert not model.add_segment(Segment("heat", START, 4.0, -3.0, -4.0, 0.0))


def test_other_mode_is_a_fallback():
    """Cooling borrows the heating segments until it has its own."""
    model = ThermalModel(clock=lambda: START)
    model.add_segment(_record(0.045, 0.0, 5.0, 20.0, 7.0))
    assert model.heat_loss("cool") == model.heat_loss("heat")
    assert not model.is_ready("cool")


def test_retention_keeps_the_newest():
    """Old segments go after RETENTION_DAYS, but the newest ones always stay."""
    model = ThermalModel(clock=lambda: START + 400 * 86400.0)
    for index in range(25):
        model.add_segment(Segment("heat", START + index, 6.0, -1.5, -60.0, 0.0))
    assert len(model.segments) == 20


def test_autonomy_matches_the_analytic_solution():
    """With constant weather the predicted time is the exponential's, within one step."""
    model = ThermalModel(clock=lambda: START)
    for _ in range(3):
        model.add_segment(Segment("heat", START, 6.0, -1.62, -36.0, 0.0))  # k = 0.045
    expected = math.log((20.4 - 10.0) / (19.7 - 10.0)) / 0.045 * 60.0
    got = model.autonomy_minutes("heat", START, 20.4, 19.7, lambda _t: (10.0, 0.0))
    assert abs(got - expected) <= 5.0


def test_autonomy_is_bounded_by_the_observed_slope():
    """A room already falling while heated is assumed to fall at least as fast."""
    model = ThermalModel()
    got = model.autonomy_minutes("heat", START, 20.4, 19.7, lambda _t: (19.0, 0.0), observed_slope=-0.7)
    assert abs(got - 60.0) < 1e-6


def test_autonomy_in_cooling_and_past_limit():
    """Cooling crosses its ceiling going up. A room already past the limit has none."""
    model = ThermalModel()
    assert model.autonomy_minutes("cool", START, 25.6, 26.3, lambda _t: (32.0, 0.0)) < 360.0
    assert model.autonomy_minutes("heat", START, 19.6, 19.7, lambda _t: (10.0, 0.0)) == 0.0


def test_autonomy_without_outdoor_holds():
    """No outdoor temperature at all: the room is assumed to hold."""
    model = ThermalModel()
    assert model.autonomy_minutes("heat", START, 20.4, 19.7, lambda _t: (None, 0.0)) == 360.0


def test_round_trip():
    """to_dict / from_dict keep segments and recoveries. Garbage is dropped."""
    model = ThermalModel(clock=lambda: START)
    model.add_segment(Segment("heat", START, 6.0, -1.62, -36.0, 0.0))
    model.add_recovery("heat", 25.0)
    data = model.to_dict()
    data["segments"].append({"bogus": 1})
    restored = ThermalModel.from_dict(data, clock=lambda: START)
    assert len(restored.segments) == 1
    assert restored.recovery_minutes("heat") == 25.0
    assert ThermalModel.from_dict(None).segments == []


def test_sensor_resolution():
    """The smallest step seen is the resolution. 0.1 until then."""
    resolution = SensorResolution()
    assert resolution.value == 0.1
    for value in (20.0, 20.0, 20.4, 20.2, 20.2):
        resolution.update(value)
    assert abs(resolution.value - 0.2) < 1e-9


def test_recovery_is_the_time_to_the_worst_reading():
    """The recovery ends at the lowest reading, confirmed one step later."""
    tracker = RecoveryTracker()
    tracker.arm("heat", START, 19.8)
    assert tracker.update(START + 600, 19.6, 0.2) is None
    assert tracker.update(START + 1500, 19.6, 0.2) is None
    assert tracker.update(START + 2400, 19.8, 0.2) == ("heat", 10.0)


def test_sun_is_up_at_noon_and_down_at_night():
    """Elevation and sun factor follow the day. Overcast lets a quarter through."""
    noon = 1_781_870_400.0  # 2026-06-19 12:00 UTC
    midnight = noon - 12 * 3600
    assert sun_elevation(45.0, 0.0, noon) > 60.0
    assert sun_factor(45.0, 0.0, midnight, 0.0) == 0.0
    clear = sun_factor(45.0, 0.0, noon, 0.0)
    overcast = sun_factor(45.0, 0.0, noon, 100.0)
    assert abs(overcast / clear - 0.25) < 1e-9


def test_evaluator_measures_the_error_of_the_model():
    """A room that loses twice what the model says ends colder than predicted, by the amount the model missed."""
    model = ThermalModel(clock=lambda: START)
    evaluator = PauseEvaluator()
    evaluator.arm("heat", START, 20.0)
    room = 20.0
    for minute in range(1, 61):
        room += 2 * DEFAULT_K * (10.0 - room) / 60.0
        evaluator.step(model, START + minute * 60.0, 10.0, 0.0)
    mode, error = evaluator.close(START + 3600.0, room)
    assert mode == "heat"
    assert math.isclose(evaluator.predicted, 10.0 + 10.0 * math.exp(-DEFAULT_K), abs_tol=0.01)
    assert math.isclose(error, room - evaluator.predicted, abs_tol=1e-9) and -0.8 < error < -0.6


def test_evaluator_ignores_short_or_broken_pauses():
    """A pause too short, a missing outdoor reading or a long gap gives no error."""
    model = ThermalModel(clock=lambda: START)
    evaluator = PauseEvaluator()
    evaluator.arm("heat", START, 20.0)
    evaluator.step(model, START + 600.0, 10.0, 0.0)
    assert evaluator.close(START + 600.0, 19.9) is None
    evaluator.arm("heat", START, 20.0)
    evaluator.step(model, START + 600.0, None, 0.0)
    assert evaluator.close(START + 3600.0, 19.0) is None
    evaluator.arm("heat", START, 20.0)
    evaluator.step(model, START + 7200.0, 10.0, 0.0)
    assert evaluator.close(START + 7200.0, 19.0) is None
    evaluator.arm("heat", START, None)
    assert evaluator.close(START + 3600.0, 19.0) is None


def test_errors_are_summarised_and_persisted():
    """The model keeps the last errors per mode: median absolute error, signed bias, round trip."""
    model = ThermalModel(clock=lambda: START)
    assert model.prediction_error("heat") is None and model.prediction_bias("heat") is None
    for error in (-0.4, 0.1, -0.2):
        model.add_error("heat", error)
    assert math.isclose(model.prediction_error("heat"), 0.2)
    assert math.isclose(model.prediction_bias("heat"), -0.2)
    assert model.prediction_error("cool") is None
    restored = ThermalModel.from_dict(model.to_dict(), clock=lambda: START)
    assert restored.errors == {"heat": [-0.4, 0.1, -0.2]}


def test_heat_loss_range_is_the_spread_of_the_stops():
    """k_min and k_max bound the learned k, None without a sunless stop."""
    model = ThermalModel(clock=lambda: START)
    assert model.heat_loss_range("heat") is None
    for k in (0.03, 0.05, 0.04):
        model.add_segment(_record(k, 0.0, 5.0, 20.0, 8.0))
    low, high = model.heat_loss_range("heat")
    assert low < model.heat_loss("heat") < high
    assert math.isclose(low, 0.03, rel_tol=0.15) and math.isclose(high, 0.05, rel_tol=0.15)
