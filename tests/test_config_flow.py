"""Config and options flows."""

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vtherm_smart_pause.const import (
    CONF_COMFORT_EXCESS,
    CONF_COMFORT_MARGIN,
    CONF_MAX_PAUSES_PER_DAY,
    CONF_SETBACK_HEAT,
    CONF_TARGET_VTHERM,
    CONF_THRESHOLD_HEAT,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    SECTION_FALLBACK_RULES,
    SECTION_GUARDS,
    SECTION_PAUSE_RULES,
    SECTION_SETBACK,
    SECTIONS,
    VTHERM_DOMAIN,
)
from custom_components.vtherm_smart_pause.manager import settings_from_config
from custom_components.vtherm_smart_pause.registry import merged_config


def _vtherm(hass, thermostat_type: str = "thermostat_over_climate") -> str:
    """Register a VTherm config entry and its climate entity. Return the entity_id."""
    vtherm_entry = MockConfigEntry(domain=VTHERM_DOMAIN, entry_id="vtherm-salon", data={"thermostat_type": thermostat_type})
    vtherm_entry.add_to_hass(hass)
    entity = er.async_get(hass).async_get_or_create("climate", VTHERM_DOMAIN, "vtherm-salon", suggested_object_id="salon", config_entry=vtherm_entry)
    hass.states.async_set(entity.entity_id, "heat", {"friendly_name": "Salon"})
    return entity.entity_id


async def test_user_flow_creates_the_entry(integration):
    """Picking an over_climate VTherm stores its unique_id."""
    entity_id = _vtherm(integration)
    result = await integration.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await integration.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET_VTHERM: entity_id, CONF_THRESHOLD_HEAT: 19.0})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Salon"
    assert result["data"] == {CONF_TARGET_VTHERM: "vtherm-salon", CONF_THRESHOLD_HEAT: 19.0}


async def test_user_flow_rejects_other_thermostats(integration):
    """An over_switch VTherm, or a climate of another integration, is refused."""
    entity_id = _vtherm(integration, "thermostat_over_switch")
    result = await integration.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await integration.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET_VTHERM: entity_id})
    assert result["errors"] == {CONF_TARGET_VTHERM: "not_over_climate"}
    result = await integration.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET_VTHERM: "climate.unknown"})
    assert result["errors"] == {CONF_TARGET_VTHERM: "not_a_vtherm"}


async def test_one_entry_per_vtherm(integration):
    """A second smart pause on the same VTherm aborts."""
    entity_id = _vtherm(integration)
    MockConfigEntry(domain=DOMAIN, data={CONF_TARGET_VTHERM: "vtherm-salon"}, unique_id=f"{DOMAIN}-vtherm-salon").add_to_hass(integration)
    result = await integration.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await integration.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET_VTHERM: entity_id})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_replace_the_data(integration):
    """Options are stored whole: a field cleared there does not come back from the data."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TARGET_VTHERM: "vtherm-salon", CONF_WEATHER_ENTITY: "weather.home"})
    entry.add_to_hass(integration)
    result = await integration.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await integration.config_entries.options.async_configure(result["flow_id"], {CONF_THRESHOLD_HEAT: 19.5, **{name: {} for name in SECTIONS}})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    conf = merged_config(entry)
    assert conf[CONF_TARGET_VTHERM] == "vtherm-salon"
    assert conf[CONF_THRESHOLD_HEAT] == 19.5
    assert CONF_WEATHER_ENTITY not in conf


async def test_sections_are_stored_nested_and_read_flat(integration):
    """The form's sections come back as flat settings, with the defaults of untouched fields."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TARGET_VTHERM: "vtherm-salon"})
    entry.add_to_hass(integration)
    result = await integration.config_entries.options.async_init(entry.entry_id)
    user_input = {
        SECTION_PAUSE_RULES: {CONF_COMFORT_MARGIN: 0.5},
        SECTION_SETBACK: {CONF_SETBACK_HEAT: 12.0},
        SECTION_GUARDS: {CONF_MAX_PAUSES_PER_DAY: 4},
        SECTION_FALLBACK_RULES: {},
    }
    result = await integration.config_entries.options.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[SECTION_PAUSE_RULES][CONF_COMFORT_MARGIN] == 0.5
    conf = merged_config(entry)
    assert conf[CONF_COMFORT_MARGIN] == 0.5
    assert conf[CONF_SETBACK_HEAT] == 12.0
    assert conf[CONF_MAX_PAUSES_PER_DAY] == 4
    assert conf[CONF_COMFORT_EXCESS] == 0.3
    assert settings_from_config(conf).comfort.margin == 0.5
    assert settings_from_config(conf).max_pauses_per_day == 4


async def test_flat_options_of_an_older_form_still_read(integration):
    """Options stored flat, before the sections existed, are read as they were."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TARGET_VTHERM: "vtherm-salon"}, options={CONF_COMFORT_MARGIN: 0.4})
    assert settings_from_config(merged_config(entry)).comfort.margin == 0.4
