"""Weather forecast cache."""

from datetime import datetime, timezone

from homeassistant.core import SupportsResponse

from custom_components.vtherm_smart_pause.forecast import WeatherForecast, parse_forecast

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc).timestamp()


def _iso(hours: float) -> str:
    """ISO timestamp *hours* after T0."""
    return datetime.fromtimestamp(T0 + hours * 3600, tz=timezone.utc).isoformat()


def test_parse_forecast_reads_clouds_or_condition():
    """cloud_coverage wins, the condition is the fallback, bad entries are dropped."""
    parsed = parse_forecast(
        [
            {"datetime": _iso(1), "temperature": 12, "condition": "cloudy"},
            {"datetime": _iso(0), "temperature": 11.5, "cloud_coverage": 20},
            {"temperature": 3},
        ]
    )
    assert parsed == [(T0, 11.5, 20.0), (T0 + 3600, 12.0, 90.0)]


async def test_forecast_is_fetched_interpolated_and_anchored(integration):
    """The hourly forecast is interpolated and shifted onto the current reading."""
    calls = []

    async def get_forecasts(call):
        """Fake weather.get_forecasts."""
        calls.append(call.data)
        return {"weather.home": {"forecast": [{"datetime": _iso(h), "temperature": 10.0 + h, "cloud_coverage": 0} for h in range(6)]}}

    integration.services.async_register("weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)
    forecast = WeatherForecast(integration, "weather.home")
    await forecast.async_refresh(T0)
    await forecast.async_refresh(T0 + 60)
    assert len(calls) == 1
    assert forecast.available
    temperature, sun = forecast.weather_at(T0 + 1800, T0, 9.0)
    assert temperature == 9.5
    assert 0.0 <= sun <= 1.0


async def test_no_forecast_keeps_the_current_reading(integration):
    """Without a weather entity the outdoor temperature stays where it is."""
    forecast = WeatherForecast(integration, None)
    await forecast.async_refresh(T0)
    assert forecast.weather_at(T0 + 7200, T0, 8.0)[0] == 8.0
