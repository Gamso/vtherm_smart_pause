"""Clear-sky sun factor from the sun's elevation and the cloud cover.

A deliberately simple proxy for solar gains: ``sin(elevation)`` is the share of
direct sunlight a horizontal surface receives, scaled down by the cloud cover.
The learned gain ``g`` (see :mod:`thermal_model`) absorbs everything else --
window orientation, blinds, shading -- so the proxy only has to rise and fall
with the real sun. The elevation uses the usual NOAA approximation, accurate to
well under a degree, which is far beyond what is needed here.

This module imports neither Home Assistant nor ``vtherm_api``.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

#: Share of the sun a fully overcast sky still lets through.
OVERCAST_TRANSMISSION = 0.25


def sun_elevation(latitude: float, longitude: float, epoch: float) -> float:
    """Return the sun's elevation in degrees at *epoch* (seconds, UTC) for a location."""
    moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    day = moment.timetuple().tm_yday
    hours = moment.hour + moment.minute / 60.0 + moment.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (day - 1 + (hours - 12.0) / 24.0)
    equation_of_time = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma) - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    solar_minutes = hours * 60.0 + equation_of_time + 4.0 * longitude
    hour_angle = math.radians(solar_minutes / 4.0 - 180.0)
    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(declination) * math.cos(hour_angle)
    return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))


def sun_factor(latitude: float, longitude: float, epoch: float, cloud_coverage: float | None) -> float:
    """Return the sun factor in [0, 1]. An unknown cloud cover counts as half covered."""
    elevation = sun_elevation(latitude, longitude, epoch)
    if elevation <= 0:
        return 0.0
    clouds = 50.0 if cloud_coverage is None else max(0.0, min(100.0, cloud_coverage))
    return math.sin(math.radians(elevation)) * (1.0 - (1.0 - OVERCAST_TRANSMISSION) * clouds / 100.0)
