"""Constants for VTherm Smart Pause. Imports nothing from Home Assistant."""

from __future__ import annotations

DOMAIN = "vtherm_smart_pause"
FEATURE_MANAGER_NAME = DOMAIN
DEVICE_NAME = "Versatile Thermostat Smart Pause"

VTHERM_DOMAIN = "versatile_thermostat"
CONF_THERMOSTAT_TYPE = "thermostat_type"
CONF_THERMOSTAT_CLIMATE = "thermostat_over_climate"
#: Suffix of the unique_id of VTherm's own auto start/stop switch (``<device name>_enable_auto_start_stop``).
VTHERM_AUTO_START_STOP_SUFFIX = "_enable_auto_start_stop"
#: VTherm reports this reason when a window opening switched it off.
HVAC_OFF_REASON_WINDOW = "hvac_off_window_detection"
#: Key, in the VTherm's own configuration, of its outdoor temperature sensor.
VTHERM_CONF_EXTERNAL_TEMP_SENSOR = "external_temperature_sensor_entity_id"
#: Command cause VTherm passes when it follows a change made on the device itself.
COMMAND_CAUSE_UNDERLYING_TRACKING = "underlying_tracking"

# Configuration keys
CONF_TARGET_VTHERM = "target_vtherm"  # unique_id of the VTherm this plugin drives
#: Per-mode setpoint threshold. On its eco side VTherm's own auto start/stop decides, on its
#: comfort side the smart pause. Empty: the smart pause always decides, in the comfort profile.
CONF_THRESHOLD_HEAT = "threshold_heat"
CONF_THRESHOLD_COOL = "threshold_cool"
CONF_WEATHER_ENTITY = "weather_entity"

#: Sections of the options form. Home Assistant stores their fields nested under these
#: keys, ``registry.merged_config`` flattens them back.
SECTION_PAUSE_RULES = "pause_rules"
SECTION_SETBACK = "setback"
SECTION_GUARDS = "guards"
SECTION_FALLBACK_RULES = "fallback_rules"
SECTIONS = (SECTION_PAUSE_RULES, SECTION_SETBACK, SECTION_GUARDS, SECTION_FALLBACK_RULES)

CONF_SETBACK_HEAT = "setback_heat"
CONF_SETBACK_COOL = "setback_cool"
CONF_COMFORT_EXCESS = "comfort_excess"
CONF_COMFORT_EXCESS_MINUTES = "comfort_excess_minutes"
CONF_COMFORT_MARGIN = "comfort_margin"
CONF_COMFORT_MIN_PAUSE = "comfort_min_pause_minutes"
CONF_ECO_EXCESS = "eco_excess"
CONF_ECO_EXCESS_MINUTES = "eco_excess_minutes"
CONF_ECO_MARGIN = "eco_margin"
CONF_ECO_MIN_PAUSE = "eco_min_pause_minutes"
CONF_MIN_RUN_MINUTES = "min_run_minutes"
CONF_MIN_OFF_MINUTES = "min_off_minutes"
CONF_MAX_PAUSES_PER_DAY = "max_pauses_per_day"
CONF_OUTDOOR_FLOOR_HEAT = "outdoor_floor_heat"
CONF_OUTDOOR_CEILING_COOL = "outdoor_ceiling_cool"

#: Setback used when the VTherm publishes no min_temp / max_temp.
FALLBACK_SETBACK_HEAT = 7.0
FALLBACK_SETBACK_COOL = 35.0

# hass.data sub-keys (see registry.py)
DATA_FACTORY_REGISTERED = "factory_registered"
DATA_MANAGERS = "managers"
DATA_ADD_ENTITIES = "add_entities"
DATA_ENTITIES = "entities"

PLATFORM_SENSOR = "sensor"
PLATFORM_SWITCH = "switch"

STORAGE_VERSION = 1
STORAGE_KEY = DOMAIN
#: The model and the pause state are saved at most this often, and on every pause or resume.
SAVE_INTERVAL_S = 600.0

#: Attribute section published in the VTherm's own state attributes.
ATTR_SECTION = "smart_pause"

#: Weather forecast refresh period and how long a stale forecast is still used.
FORECAST_REFRESH_S = 1800.0
FORECAST_MAX_AGE_S = 3 * 3600.0

ENTITY_UNIQUE_ID_PREFIX = DOMAIN


def build_unique_id(object_key: str, entry_id: str) -> str:
    """Build the canonical unique_id for an entity."""
    return f"{ENTITY_UNIQUE_ID_PREFIX}_{object_key}_{entry_id}"
