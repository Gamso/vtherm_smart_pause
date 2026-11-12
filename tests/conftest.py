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

DEPRECATION_MARKERS = ("deprecated", "will stop working", "is being removed")


@pytest.fixture
def integration(hass, caplog):
    """Yield a hass with a clean VThermAPI. Fail on any deprecation logged about this integration."""
    caplog.set_level(logging.WARNING)
    yield hass
    VThermAPI.reset_vtherm_api()
    offending = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and DOMAIN in record.getMessage() and any(marker in record.getMessage().lower() for marker in DEPRECATION_MARKERS)
    ]
    assert not offending, offending


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

    @property
    def now(self) -> datetime:
        """VTherm's clock."""
        return datetime.fromtimestamp(self.epoch, tz=timezone.utc)

    def advance(self, minutes: float) -> None:
        """Move the clock forward."""
        self.epoch += minutes * 60.0


def make_entry(target: str = "vtherm-salon", **data):
    """Build a MockConfigEntry targeting *target*."""
    from pytest_homeassistant.common import MockConfigEntry  # pylint: disable=import-outside-toplevel

    return MockConfigEntry(domain=DOMAIN, data={CONF_TARGET_VTHERM: target, **data}, unique_id=f"{DOMAIN}-{target}", title="Salon")
