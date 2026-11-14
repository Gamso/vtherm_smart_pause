"""Tests of the pause decision engine."""

from custom_components.vtherm_smart_pause.decision import (
    Action,
    DecisionEngine,
    Inputs,
    Profile,
    Settings,
    State,
)

COMFORT = Profile(excess=0.3, excess_minutes=20.0, margin=0.3, min_pause_minutes=60.0)
ECO = Profile(excess=0.2, excess_minutes=10.0, margin=1.0, min_pause_minutes=30.0)


def _inputs(minute: float, room: float, setpoint: float = 20.0, hvac_mode: str = "heat", **kwargs) -> Inputs:
    """Build the inputs of one cycle at *minute* of day 1."""
    defaults = {"recovery_minutes": 30.0, "outdoor": 10.0}
    defaults.update(kwargs)
    return Inputs(now=minute * 60.0, day=1, hvac_mode=hvac_mode, setpoint=setpoint, room=room, **defaults)


def _long(*_args) -> float:
    """Predictor: the room holds for the whole horizon."""
    return 360.0


def _engine(**kwargs) -> DecisionEngine:
    """Build an engine with the test profiles."""
    return DecisionEngine(Settings(comfort=COMFORT, eco=ECO, **kwargs))


def _run(engine: DecisionEngine, start: int, end: int, room: float, predict=_long, **kwargs):
    """Step the engine every minute from *start* to *end* and return the decisions."""
    return [engine.step(_inputs(m, room, **kwargs), predict) for m in range(start, end)]


def test_pauses_after_excess_held_long_enough():
    """A room 0.4 °C over the setpoint for 20 minutes is paused, not before."""
    engine = _engine()
    decisions = _run(engine, 0, 21, 20.4)
    assert all(d.action is None for d in decisions[:20])
    assert decisions[20].action == Action.PAUSE
    assert engine.state == State.PAUSED


def test_reading_exactly_at_threshold_counts():
    """20.0 - 20.3 is -0.2999...: a reading at the threshold must still count as excess."""
    engine = _engine()
    decisions = _run(engine, 0, 21, 20.3)
    assert decisions[-1].action == Action.PAUSE


def test_small_excess_never_pauses():
    """0.2 °C over the setpoint is below the comfort excess."""
    engine = _engine()
    assert all(d.action is None for d in _run(engine, 0, 120, 20.2))


def test_comfort_profile_does_not_wait_for_the_model():
    """Both profiles pause on the predicted autonomy alone: an unlearned model predicts with its prudent default."""
    engine = _engine(threshold_heat=19.0)
    decisions = _run(engine, 0, 30, 20.5)
    assert any(d.action == Action.PAUSE for d in decisions)
    assert decisions[-1].profile == "comfort"
    eco_engine = _engine(threshold_heat=19.0)
    decisions = _run(eco_engine, 0, 30, 17.8, setpoint=17.5)
    assert any(d.action == Action.PAUSE for d in decisions)
    assert decisions[-1].profile == "eco"


def test_short_autonomy_blocks():
    """A room predicted to reach its limit soon is not paused."""
    engine = _engine()
    decisions = _run(engine, 0, 40, 20.4, predict=lambda *_: 45.0)
    assert decisions[-1].reason == "short_autonomy"


def test_outdoor_bounds_block():
    """No pause in heating when it is too cold outside."""
    engine = _engine()
    decisions = _run(engine, 0, 40, 20.4, outdoor=-8.0)
    assert decisions[-1].reason == "outdoor_too_cold"


def test_resumes_on_comfort():
    """A paused room that falls past half the margin is resumed at once."""
    engine = _engine()
    _run(engine, 0, 21, 20.4)
    decision = engine.step(_inputs(22, 19.85), _long)
    assert decision.action == Action.RESUME
    assert decision.reason == "comfort"


def test_resumes_on_predicted_crossing_after_min_off():
    """The predicted crossing resumes the pause, but only once the shortest pause has passed."""
    engine = _engine()
    _run(engine, 0, 21, 20.4)
    early = engine.step(_inputs(25, 20.2), lambda *_: 20.0)
    assert early.action is None
    late = engine.step(_inputs(45, 20.2), lambda *_: 20.0)
    assert late.action == Action.RESUME
    assert late.reason == "autonomy"


def test_min_run_after_resume():
    """After a resume the next pause waits for the minimum run time."""
    engine = _engine()
    _run(engine, 0, 21, 20.4)
    engine.step(_inputs(22, 19.8), _long)
    decisions = _run(engine, 23, 50, 20.4)
    assert all(d.action is None for d in decisions[:20])
    assert any(d.action == Action.PAUSE for d in decisions)


def test_daily_pause_cap():
    """The number of pauses per day is capped."""
    engine = _engine(max_pauses_per_day=1, min_run_minutes=0.0)
    _run(engine, 0, 21, 20.4)
    engine.step(_inputs(22, 19.8), _long)
    decisions = _run(engine, 23, 80, 20.4)
    assert decisions[-1].reason == "max_pauses"


def test_inactive_releases_the_pause():
    """A window opening (or VTherm off) ends the pause and stops deciding."""
    engine = _engine()
    _run(engine, 0, 21, 20.4)
    decision = engine.step(_inputs(22, 20.4, inactive_reason="window"), _long)
    assert decision.action == Action.RESUME
    assert engine.state == State.INACTIVE
    off = engine.step(_inputs(23, 20.4, hvac_mode="off"), _long)
    assert off.action is None
    assert off.reason == "hvac_mode"


def test_setpoint_rise_resumes():
    """A user raising the setpoint past the excess resumes. A lower one keeps the pause."""
    engine = _engine()
    _run(engine, 0, 21, 20.4)
    kept = engine.step(_inputs(22, 20.4, setpoint=19.0), _long)
    assert kept.action is None
    raised = engine.step(_inputs(23, 20.4, setpoint=20.5), _long)
    assert raised.action == Action.RESUME
    assert raised.reason == "setpoint_change"


def test_cool_mirrors_heat():
    """Cooling pauses a room below its setpoint and resumes it when it warms."""
    engine = _engine()
    decisions = _run(engine, 0, 21, 25.6, setpoint=26.0, hvac_mode="cool", outdoor=30.0)
    assert decisions[-1].action == Action.PAUSE
    assert decisions[-1].limit == 26.3
    resumed = engine.step(_inputs(22, 26.2, setpoint=26.0, hvac_mode="cool", outdoor=30.0), _long)
    assert resumed.action == Action.RESUME


def test_cool_threshold_selects_profile():
    """In cooling, a setpoint above the threshold is eco."""
    engine = _engine(threshold_cool=26.0)
    assert engine.profile_for("cool", 25.0)[0] == "comfort"
    assert engine.profile_for("cool", 28.0)[0] == "eco"
    assert engine.profile_for("heat", 28.0)[0] == "comfort"


def test_restore_keeps_a_pause():
    """A pause survives a restart through to_dict / restore."""
    engine = _engine()
    _run(engine, 0, 21, 20.4)
    restored = _engine()
    restored.restore(engine.to_dict())
    assert restored.state == State.PAUSED
    assert restored.pauses_today == 1
    decision = restored.step(_inputs(22, 19.8), _long)
    assert decision.action == Action.RESUME


def test_restore_ignores_garbage():
    """Unreadable stored data leaves a fresh engine."""
    engine = _engine()
    engine.restore({"state": "paused", "paused_since": "x"})
    engine.restore(None)
    assert engine.state == State.RUNNING
