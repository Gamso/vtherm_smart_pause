"""Hourly weather forecast and sun factor for the autonomy prediction.

The forecast comes from ``weather.get_forecasts`` (``type: hourly``), refreshed
every 30 minutes. A failed refresh keeps the last one for three hours. Any
weather integration works: the temperature is required, the cloud cover is used
when the integration publishes ``cloud_coverage``, otherwise read off the
``condition``. Without a forecast the outdoor temperature stays at its current
value and the sky counts as half covered.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import FORECAST_MAX_AGE_S, FORECAST_REFRESH_S
from .log import get_logger
from .solar import sun_factor

_LOGGER = get_logger(__name__)

#: Cloud cover (%) assumed for a weather condition when no cloud_coverage is published.
CONDITION_CLOUDS = {
    "sunny": 0.0,
    "clear-night": 0.0,
    "partlycloudy": 50.0,
    "windy": 30.0,
    "windy-variant": 60.0,
    "cloudy": 90.0,
    "fog": 100.0,
    "rainy": 90.0,
    "pouring": 100.0,
    "snowy": 100.0,
    "snowy-rainy": 100.0,
    "hail": 100.0,
    "lightning": 90.0,
    "lightning-rainy": 100.0,
    "exceptional": 80.0,
}


def parse_forecast(items: list[dict[str, Any]]) -> list[tuple[float, float | None, float | None]]:
    """Turn forecast entries into sorted ``(epoch, temperature, cloud %)``. Bad entries are skipped."""
    parsed = []
    for item in items or []:
        try:
            moment = datetime.fromisoformat(str(item["datetime"])).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        temperature = item.get("temperature")
        temperature = float(temperature) if isinstance(temperature, (int, float)) else None
        clouds = item.get("cloud_coverage")
        if not isinstance(clouds, (int, float)):
            clouds = CONDITION_CLOUDS.get(str(item.get("condition")))
        parsed.append((moment, temperature, float(clouds) if clouds is not None else None))
    return sorted(parsed)


class WeatherForecast:
    """Forecast cache of one weather entity (or none)."""

    def __init__(self, hass: HomeAssistant, entity_id: str | None) -> None:
        self._hass = hass
        self._entity_id = entity_id
        self._entries: list[tuple[float, float | None, float | None]] = []
        self._fetched_at: float | None = None
        self._tried_at: float | None = None

    @property
    def available(self) -> bool:
        """Whether a usable forecast is held."""
        return bool(self._entries)

    async def async_refresh(self, now: float) -> None:
        """Fetch the hourly forecast when the last attempt is old enough."""
        if self._entity_id is None or (self._tried_at is not None and now - self._tried_at < FORECAST_REFRESH_S):
            return
        self._tried_at = now
        try:
            response = await self._hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": self._entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.debug("Forecast of %s unavailable: %s", self._entity_id, err)
            self._expire(now)
            return
        entries = parse_forecast(((response or {}).get(self._entity_id) or {}).get("forecast") or [])
        if entries:
            self._entries, self._fetched_at = entries, now
        else:
            self._expire(now)

    def _expire(self, now: float) -> None:
        """Drop a forecast older than FORECAST_MAX_AGE_S."""
        if self._fetched_at is not None and now - self._fetched_at > FORECAST_MAX_AGE_S:
            self._entries, self._fetched_at = [], None

    def _around(self, epoch: float) -> tuple[float | None, float | None]:
        """Interpolated temperature and cloud cover at *epoch*, None outside the forecast."""
        entries = self._entries
        if not entries or epoch < entries[0][0] - 3600.0 or epoch > entries[-1][0] + 3600.0:
            return None, None
        before = [e for e in entries if e[0] <= epoch]
        after = [e for e in entries if e[0] > epoch]
        if not before:
            return entries[0][1], entries[0][2]
        if not after:
            return before[-1][1], before[-1][2]
        (t0, temp0, cloud0), (t1, temp1, cloud1) = before[-1], after[0]
        ratio = (epoch - t0) / (t1 - t0)
        temperature = temp0 + ratio * (temp1 - temp0) if temp0 is not None and temp1 is not None else (temp0 if temp0 is not None else temp1)
        return temperature, cloud0 if cloud0 is not None else cloud1

    def weather_at(self, epoch: float, now: float, current_outdoor: float | None) -> tuple[float | None, float]:
        """Outdoor temperature and sun factor at *epoch*.

        The current reading wins at ``now``. The forecast is shifted so that it
        starts from it, which removes the bias between the station and the local
        sensor without discarding the trend.
        """
        temperature, clouds = self._around(epoch)
        if temperature is not None and current_outdoor is not None:
            forecast_now, _ = self._around(now)
            if forecast_now is not None:
                temperature += current_outdoor - forecast_now
        if temperature is None:
            temperature = current_outdoor
        config = self._hass.config
        return temperature, sun_factor(config.latitude, config.longitude, epoch, clouds)
