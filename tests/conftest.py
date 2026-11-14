"""Shared fixtures.

The pure modules (decision, thermal model, solar, replay) need no
event loop. The Home Assistant tests use the ``hass`` fixture of
pytest-homeassistant through ``integration`` below, which also fails a test
when Home Assistant logs a deprecation for this integration: 2026.11 is the
floor, so nothing deprecated there may be used.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from vtherm_api.vtherm_api import VThermAPI

from custom_components.vtherm_smart_pause.const import CONF_TARGET_VTHERM, DOMAIN
from custom_components.vtherm_smart_pause.registry import managers

DEPRECATION_MARKERS = ("deprecated", "will stop working", "is being removed")


@pytest.fixture
def integration(hass, caplog):
    """Yield a hass with a clean VThermAPI. Fail on any deprecation logged about this integration."""
    caplog.set_level(logging.WARNING)
    yield hass
    # VTherm stops its managers when it unloads: so do the tests, which drops their pending timers.
    for manager in list(managers(hass).values()):
        manager.stop_listening()
    VThermAPI.reset_vtherm_api()
    offending = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and DOMAIN in record.getMessage() and any(marker in record.getMessage().lower() for marker in DEPRECATION_MARKERS)
    ]
    assert not offending, offending


class FakeHvacMode:
    """A look-alike of VTherm's ``VThermHvacMode``: equal to its string, and unhashable.

    Defining ``__eq__`` without ``__hash__`` is what makes the real class
    unusable as a dict key, so the fake must keep that trap.
    """

    def __init__(self, hvac_mode: str) -> None:
        self._hvac_mode = hvac_mode

    def __str__(self) -> str:
        return self._hvac_mode

    def __eq__(self, other: object) -> bool:
        return self._hvac_mode == str(other)


@dataclass
class FakeRuntime:  # pylint: disable=too-many-instance-attributes
    """A stand-in for VTherm's InterfaceThermostatRuntime, with settable readings."""

    hass: object
    unique_id: str = "vtherm-salon"
    name: str = "Salon"
    entity_id: str = "climate.salon"
    entry_infos: dict = field(default_factory=lambda: {"thermostat_type": "thermostat_over_climate"})
    vtherm_hvac_mode: str | None = "heat"
    requested_hvac_mode: str | None = "heat"
    current_temperature: float | None = 20.0
    current_outdoor_temperature: float | None = 10.0
    target_temperature: float | None = 20.0
    regulated_target_temperature: float | None = 20.0
    last_temperature_slope: float | None = 0.0
    hvac_action: str | None = None
    hvac_off_reason: str | None = None
    is_overpowering_detected: bool = False
    cycle_min: int = 5
    epoch: float = 1_762_000_000.0
    #: The ``force`` of every control cycle the plugin asked VTherm for.
    control_calls: list = field(default_factory=list)

    def __getattribute__(self, name: str):
        """Return the HVAC modes as VTherm does: as mode objects, not strings."""
        value = super().__getattribute__(name)
        if name in ("vtherm_hvac_mode", "requested_hvac_mode") and isinstance(value, str):
            return FakeHvacMode(value)
        return value

    @property
    def now(self) -> datetime:
        """VTherm's clock."""
        return datetime.fromtimestamp(self.epoch, tz=timezone.utc)

    async def async_control_heating(self, timestamp=None, force: bool = False) -> bool:
        """Record a control cycle asked by the plugin."""
        del timestamp
        self.control_calls.append(force)
        return True

    def advance(self, minutes: float) -> None:
        """Move the clock forward."""
        self.epoch += minutes * 60.0


def make_entry(target: str = "vtherm-salon", **data):
    """Build a MockConfigEntry targeting *target*."""
    from pytest_homeassistant.common import MockConfigEntry  # pylint: disable=import-outside-toplevel

    return MockConfigEntry(domain=DOMAIN, data={CONF_TARGET_VTHERM: target, **data}, unique_id=f"{DOMAIN}-{target}", title="Salon")
