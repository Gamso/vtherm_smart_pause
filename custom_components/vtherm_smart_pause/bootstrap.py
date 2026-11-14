"""Read the recorder history of one thermostat and learn its stops from it.

Home Assistant's recorder keeps the VTherm's HVAC mode and room temperature
(its state and its ``current_temperature`` attribute) and the outdoor
temperature. The learning itself is :mod:`history_learning`, shared with the
live learning, this module only fetches and converts the recorded states.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial

from homeassistant.components.recorder import get_instance, history
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .history_learning import Series, learn_segments
from .log import get_logger
from .solar import sun_factor
from .thermal_model import Segment

_LOGGER = get_logger(__name__)

#: How far back the history is read, the same span as the model's retention window allows.
BOOTSTRAP_DAYS = 10


def _number(value) -> float | None:
    """Return *value* as a float, None when it is not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _usable(state: State) -> bool:
    """Whether a recorded state carries a value."""
    return state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def mode_series(states: list[State]) -> Series:
    """The thermostat's HVAC mode over time (its state)."""
    return [(state.last_updated.timestamp(), state.state if _usable(state) else None) for state in states]


def room_series(states: list[State]) -> Series:
    """The room temperature over time (the thermostat's ``current_temperature`` attribute)."""
    return [(state.last_updated.timestamp(), _number(state.attributes.get("current_temperature"))) for state in states]


def window_series(states: list[State]) -> Series:
    """The window sensor's state over time, None while it is unknown."""
    return [(state.last_updated.timestamp(), state.state if _usable(state) else None) for state in states]


def outdoor_series(states: list[State], attribute: str | None = None) -> Series:
    """The outdoor temperature over time, from a sensor's state or a weather entity's attribute."""
    return [(state.last_updated.timestamp(), _number(state.attributes.get(attribute) if attribute else state.state) if _usable(state) else None) for state in states]


async def async_history_segments(
    hass: HomeAssistant,
    vtherm_entity_id: str,
    outdoor_entity: str | None,
    weather_entity: str | None,
    now: float,
    days: int = BOOTSTRAP_DAYS,
    window_entity: str | None = None,
) -> list[Segment]:
    """Return the stops of the thermostat in the last *days* days, learned from the recorder.

    The outdoor temperature comes from the thermostat's own outdoor sensor,
    else from the weather entity. The stops with the thermostat's window sensor
    open are dropped. Returns nothing when the recorder is not loaded or holds
    nothing for the thermostat.
    """
    if "recorder" not in hass.config.components:
        return []
    end = dt_util.utc_from_timestamp(now)
    start = end - timedelta(days=days)
    entity_ids = [entity_id for entity_id in (vtherm_entity_id, outdoor_entity, weather_entity, window_entity) if entity_id]
    # The recorder's database is read in its own executor, never on the event loop.
    recorded = await get_instance(hass).async_add_executor_job(partial(history.get_significant_states, hass, start, end, entity_ids, None, True, False, False, False))
    thermostat = recorded.get(vtherm_entity_id) or []
    outdoors = outdoor_series(recorded.get(outdoor_entity) or []) if outdoor_entity else []
    if not any(isinstance(value, float) for _, value in outdoors) and weather_entity:
        outdoors = outdoor_series(recorded.get(weather_entity) or [], "temperature")
    if not thermostat or not outdoors:
        return []

    latitude, longitude = hass.config.latitude, hass.config.longitude
    return learn_segments(
        mode_series(thermostat),
        room_series(thermostat),
        outdoors,
        start.timestamp(),
        end.timestamp(),
        lambda epoch: sun_factor(latitude, longitude, epoch, None),
        window_series(recorded.get(window_entity) or []) if window_entity else None,
    )
