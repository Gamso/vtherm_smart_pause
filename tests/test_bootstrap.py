"""The history bootstrap: the recorder is read once, while the model is empty."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import FakeRuntime, make_entry
from homeassistant.core import State
from replay import load_rows

from custom_components.vtherm_smart_pause.const import CONF_WEATHER_ENTITY
from custom_components.vtherm_smart_pause.manager import SmartPauseFeatureManager

THERMOSTAT = "climate.salon"
OUTDOOR = "sensor.outdoor"
WEATHER = "weather.home"
WINDOW = "binary_sensor.salon_window"


def _state(entity_id: str, state: str, epoch: float, **attributes) -> State:
    """A recorded state at *epoch*."""
    moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return State(entity_id, state, attributes, last_changed=moment, last_updated=moment)


def _recorded(rows: list[dict]) -> dict[str, list[State]]:
    """The recorder's answer for the dataset: a state wherever the mode or the room changes."""
    thermostat, outdoor, weather = [], [], []
    last_thermostat = last_outdoor = None
    for row in rows:
        key = (row["mode"], row["room"])
        if key != last_thermostat:
            thermostat.append(_state(THERMOSTAT, row["mode"] or "unavailable", row["t"], current_temperature=row["room"]))
            last_thermostat = key
        if row["outdoor"] != last_outdoor and row["outdoor"] is not None:
            outdoor.append(_state(OUTDOOR, str(row["outdoor"]), row["t"]))
            weather.append(_state(WEATHER, "cloudy", row["t"], temperature=row["outdoor"]))
            last_outdoor = row["outdoor"]
    return {THERMOSTAT: thermostat, OUTDOOR: outdoor, WEATHER: weather}


async def _manager(hass, recorded, outdoor_sensor: bool = True, window_sensor: bool = False, **data) -> tuple[SmartPauseFeatureManager, FakeRuntime, MagicMock]:
    """Build a manager whose recorder returns *recorded*. Return it with the mocked history call."""
    rows = load_rows()
    make_entry(**{CONF_WEATHER_ENTITY: WEATHER, **data}).add_to_hass(hass)
    hass.states.async_set(THERMOSTAT, "heat", {"min_temp": 16.0})
    hass.config.components.add("recorder")
    entry_infos = {"thermostat_type": "thermostat_over_climate"}
    if outdoor_sensor:
        entry_infos["external_temperature_sensor_entity_id"] = OUTDOOR
    if window_sensor:
        entry_infos["window_sensor_entity_id"] = WINDOW
    runtime = FakeRuntime(hass, entity_id=THERMOSTAT, entry_infos=entry_infos)
    runtime.epoch = rows[-1]["t"] + 60.0
    fetch = MagicMock(return_value=recorded)
    manager = SmartPauseFeatureManager(runtime, hass)
    manager.post_init(runtime.entry_infos)
    await manager.start_listening()
    return manager, runtime, fetch


def _patched(fetch: MagicMock):
    """Patch the recorder's executor and its history call."""
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(side_effect=lambda func: func())
    return (
        patch("custom_components.vtherm_smart_pause.bootstrap.get_instance", return_value=instance),
        patch("custom_components.vtherm_smart_pause.bootstrap.history.get_significant_states", fetch),
    )


async def test_first_cycle_learns_from_the_history(integration):
    """With an empty model, the first cycle reads the recorder and the comfort side is ready."""
    manager, _, fetch = await _manager(integration, _recorded(load_rows()))
    instance, history = _patched(fetch)
    with instance, history:
        await manager.refresh_state()
    assert fetch.call_count == 1
    assert len(manager.model.segments) >= 10
    assert manager.history_segments == len(manager.model.segments)
    assert manager.model.is_ready("heat")
    assert 0.02 <= manager.model.heat_loss("heat") <= 0.06
    attributes: dict = {}
    manager.add_custom_attributes(attributes)
    assert attributes["smart_pause"]["history_segments"] == manager.history_segments


async def test_the_history_is_read_only_once(integration):
    """Later cycles, and a restart with a learned model, do not read the recorder again."""
    manager, runtime, fetch = await _manager(integration, _recorded(load_rows()))
    instance, history = _patched(fetch)
    with instance, history:
        await manager.refresh_state()
        runtime.advance(5)
        await manager.refresh_state()
        restored = SmartPauseFeatureManager(runtime, integration)
        await restored.start_listening()
        await restored.refresh_state()
    assert fetch.call_count == 1
    assert restored.history_segments == manager.history_segments


async def test_weather_is_the_fallback_outdoor_source(integration):
    """Without an outdoor sensor on the thermostat, the weather entity's temperature is used."""
    manager, _, fetch = await _manager(integration, _recorded(load_rows()), outdoor_sensor=False)
    instance, history = _patched(fetch)
    with instance, history:
        await manager.refresh_state()
    assert manager.model.is_ready("heat")
    assert WEATHER in fetch.call_args.args[3]


async def test_an_empty_history_changes_nothing(integration):
    """A recorder with nothing for the thermostat leaves the model empty and the cycle running."""
    manager, _, fetch = await _manager(integration, {})
    instance, history = _patched(fetch)
    with instance, history:
        await manager.refresh_state()
    assert not manager.model.segments
    assert manager.history_segments == 0
    assert manager.decision is not None


async def test_a_recorder_failure_is_survived(integration, caplog):
    """An error reading the history is logged, the live learning goes on."""
    manager, _, fetch = await _manager(integration, {})
    fetch.side_effect = RuntimeError("database locked")
    instance, history = _patched(fetch)
    with instance, history:
        await manager.refresh_state()
        await manager.refresh_state()
    assert "could not read the history" in caplog.text
    assert fetch.call_count == 1
    assert manager.decision is not None


async def test_without_the_recorder_nothing_is_read(integration):
    """With the recorder not loaded, the history is not queried at all."""
    manager, _, fetch = await _manager(integration, _recorded(load_rows()))
    integration.config.components.remove("recorder")
    instance, history = _patched(fetch)
    with instance, history:
        await manager.refresh_state()
    assert fetch.call_count == 0
    assert not manager.model.segments


async def test_stops_with_the_window_open_are_not_learned(integration):
    """With the VTherm's window sensor open all along, no stop is learned from the history."""
    rows = load_rows()
    recorded = _recorded(rows)
    recorded[WINDOW] = [_state(WINDOW, "on", rows[0]["t"])]
    manager, _, fetch = await _manager(integration, recorded, window_sensor=True)
    instance, history = _patched(fetch)
    with instance, history:
        await manager.refresh_state()
    assert WINDOW in fetch.call_args.args[3]
    assert not manager.model.segments


async def test_a_closed_window_keeps_the_history(integration):
    """A window sensor that stayed closed learns the same stops as no window sensor at all."""
    rows = load_rows()
    recorded = _recorded(rows)
    recorded[WINDOW] = [_state(WINDOW, "off", rows[0]["t"])]
    manager, _, fetch = await _manager(integration, recorded, window_sensor=True)
    instance, history = _patched(fetch)
    with instance, history:
        await manager.refresh_state()
    assert len(manager.model.segments) >= 10
