"""Learning the heat loss from recorded series, on the November 2026 dataset and on small cases."""

import pytest
from replay import LATITUDE, LONGITUDE, load_rows

from custom_components.vtherm_smart_pause.history_learning import learn_segments
from custom_components.vtherm_smart_pause.solar import sun_factor
from custom_components.vtherm_smart_pause.thermal_model import ThermalModel

HOUR = 3600.0


def _sun(epoch: float) -> float:
    """The sun proxy with the clouds unknown, as the history bootstrap uses."""
    return sun_factor(LATITUDE, LONGITUDE, epoch, None)


def _series(rows: list[dict], key: str) -> list[tuple[float, object]]:
    """One column of the replay rows as an event series, a row only where the value changes."""
    series, last = [], object()
    for row in rows:
        if row[key] != last:
            series.append((row["t"], row[key]))
            last = row[key]
    return series


@pytest.fixture(scope="module")
def rows():
    """The ten days of recorded data."""
    return load_rows()


@pytest.fixture(scope="module")
def learned(rows):
    """The model learned from the whole dataset, as the bootstrap would."""
    segments = learn_segments(_series(rows, "mode"), _series(rows, "room"), _series(rows, "outdoor"), rows[0]["t"], rows[-1]["t"], _sun)
    model = ThermalModel(clock=lambda: rows[-1]["t"])
    for segment in segments:
        model.add_segment(segment)
    return model


def test_the_nights_are_found(learned):
    """Ten days hold at least ten usable stops, and the comfort side is ready at once."""
    assert len(learned.segments) >= 10
    assert learned.sunless_count("heat") >= 3
    assert learned.is_ready("heat")


def test_heat_loss_matches_the_live_learning(learned):
    """k from the history falls in the range read off the data, as the live replay finds."""
    assert 0.02 <= learned.heat_loss("heat") <= 0.06


def test_a_stop_is_only_the_thermostat_off(rows):
    """Fan only, dry or an unavailable thermostat are not stops."""
    start = rows[0]["t"]
    room = [(start, 20.0), (start + 8 * HOUR, 18.0)]
    outdoor = [(start, 5.0)]
    off = [(start, "off")]
    assert learn_segments(off, room, outdoor, start, start + 8 * HOUR, lambda _t: 0.0)
    for mode in ("fan_only", "dry", "heat", None):
        assert not learn_segments([(start, mode)], room, outdoor, start, start + 8 * HOUR, lambda _t: 0.0)


def test_an_interruption_ends_the_stop(rows):
    """A thermostat switched on in the middle splits the stop, and a short half teaches nothing."""
    start = rows[0]["t"]
    room = [(start, 20.0), (start + 4 * HOUR, 19.0), (start + 9 * HOUR, 18.0)]
    outdoor = [(start, 5.0)]
    modes = [(start, "off"), (start + 4.5 * HOUR, "heat")]
    segments = learn_segments(modes, room, outdoor, start, start + 6 * HOUR, lambda _t: 0.0)
    assert len(segments) == 1
    assert segments[0].hours < 4.5


def test_no_outdoor_temperature_no_segment(rows):
    """Without an outdoor temperature nothing can be learned."""
    start = rows[0]["t"]
    room = [(start, 20.0), (start + 8 * HOUR, 18.0)]
    assert not learn_segments([(start, "off")], room, [], start, start + 8 * HOUR, lambda _t: 0.0)


def test_a_stop_still_going_on_is_closed_at_the_end(rows):
    """The night in progress counts."""
    start = rows[0]["t"]
    room = [(start, 20.4), (start + 3 * HOUR, 19.8), (start + 6 * HOUR, 19.2)]
    segments = learn_segments([(start, "off")], room, [(start, 5.0)], start, start + 6 * HOUR, lambda _t: 0.0)
    assert len(segments) == 1
    assert segments[0].mean_sun == 0.0


def test_a_stop_with_the_window_open_is_dropped(rows):
    """A window open at any time during a stop drops it whole, and the next stop is learned again."""
    start = rows[0]["t"]
    room = [(start, 20.4), (start + 3 * HOUR, 19.8), (start + 6 * HOUR, 19.2), (start + 12 * HOUR, 20.4), (start + 18 * HOUR, 19.2)]
    modes = [(start, "off"), (start + 6 * HOUR, "heat"), (start + 12 * HOUR, "off")]
    windows = [(start, "off"), (start + 2 * HOUR, "on"), (start + 2.5 * HOUR, "off")]
    segments = learn_segments(modes, room, [(start, 5.0)], start, start + 18 * HOUR, lambda _t: 0.0, windows)
    assert len(segments) == 1
    assert segments[0].end > start + 12 * HOUR


def test_a_closed_or_unknown_window_changes_nothing(rows):
    """Closed, unknown or without a window sensor, the stop is learned."""
    start = rows[0]["t"]
    room = [(start, 20.4), (start + 3 * HOUR, 19.8), (start + 6 * HOUR, 19.2)]
    for windows in (None, [], [(start, "off")], [(start, None)]):
        assert len(learn_segments([(start, "off")], room, [(start, 5.0)], start, start + 6 * HOUR, lambda _t: 0.0, windows)) == 1
