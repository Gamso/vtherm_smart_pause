"""Shared accessors for the plugin registries stored in ``hass.data``.

VTherm creates one manager per eligible thermostat, while Home Assistant sets
up the entity platforms independently and in no guaranteed order. These
helpers give both sides one rendez-vous point, keyed by the VTherm
``unique_id``, so whichever arrives second picks up what the first published.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .const import CONF_TARGET_VTHERM, DATA_ADD_ENTITIES, DATA_ENTITIES, DATA_MANAGERS, DOMAIN, SECTIONS

if TYPE_CHECKING:
    from .manager import SmartPauseFeatureManager


def merged_config(entry: Any) -> dict[str, Any]:
    """A config entry's settings: its options once saved, else its creation data.

    Options replace the data rather than being merged over it: a field cleared
    in the options form (an entity removed) must not come back from the data.
    The fields of the options form's sections are stored nested under the
    section's key and come back flat here, as the flat options of an older
    version of the form do.
    """
    source = {CONF_TARGET_VTHERM: entry.data.get(CONF_TARGET_VTHERM), **entry.options} if entry.options else dict(entry.data)
    flat: dict[str, Any] = {}
    for key, value in source.items():
        if key in SECTIONS and isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return flat


def domain_data(hass: HomeAssistant) -> dict[str, Any]:
    """Return the plugin storage in hass.data, creating it if needed."""
    return hass.data.setdefault(DOMAIN, {})


def managers(hass: HomeAssistant) -> dict[str, "SmartPauseFeatureManager"]:
    """Return the live managers keyed by VTherm unique_id."""
    return domain_data(hass).setdefault(DATA_MANAGERS, {})


def get_manager(hass: HomeAssistant, unique_id: str) -> "SmartPauseFeatureManager | None":
    """Return the manager bound to a VTherm unique_id, if any."""
    return managers(hass).get(unique_id)


def add_entities_registry(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Return the ``async_add_entities`` callbacks by VTherm unique_id."""
    return domain_data(hass).setdefault(DATA_ADD_ENTITIES, {})


def entity_bucket(hass: HomeAssistant, unique_id: str) -> dict[str, Any]:
    """Return the created entities of one VTherm, by platform."""
    return domain_data(hass).setdefault(DATA_ENTITIES, {}).setdefault(unique_id, {})


def clear_registry(hass: HomeAssistant, unique_id: str) -> None:
    """Drop every stored reference for one VTherm.

    Call this only from ``async_unload_entry``, once the platforms have torn the
    entities down -- not from ``stop_listening()``, which VTherm also calls when
    it merely restarts the same manager: clearing there would make the next
    cycle add duplicates of live entities.
    """
    managers(hass).pop(unique_id, None)
    add_entities_registry(hass).pop(unique_id, None)
    domain_data(hass).setdefault(DATA_ENTITIES, {}).pop(unique_id, None)
