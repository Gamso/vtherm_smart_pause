"""Replay of the November 2026 dataset (docs/study.md, §8).

The numbers pinned here are the measured ones with the default settings. A
change to the decision or the model that moves them needs docs/study.md (§1.1
and §8) updated with it, not just these bounds.
"""

import pytest

from replay import load_rows, run_replay


@pytest.fixture(scope="module")
def result():
    """Run the reference replay once for the module."""
    return run_replay(rows=load_rows())


def test_heat_loss_is_learned_from_the_nights(result):
    """k from the real nights falls in the range read off the data (docs/study.md, §1.1)."""
    assert result.sunless_segments >= 3
    assert 0.02 <= result.heat_loss <= 0.06


def test_pauses_capture_the_wasted_energy(result):
    """The pauses cover most of the 7.0 kWh drawn with the room past the setpoint."""
    assert result.paused_energy_kwh >= 5.5


def test_comfort_limit_is_never_crossed(result):
    """The simulated room never reaches the comfort limit while paused."""
    assert result.worst_limit_margin > 0.0


def test_compressor_is_spared(result):
    """Few pauses, few per day."""
    assert result.pauses <= 20
    assert result.max_pauses_per_day <= 4
