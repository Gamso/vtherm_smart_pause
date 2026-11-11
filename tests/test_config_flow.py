"""Config and options flows."""

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vtherm_smart_pause.const import CONF_TARGET_VTHERM, CONF_THRESHOLD_HEAT, CONF_WEATHER_ENTITY, DOMAIN, VTHERM_DOMAIN
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
    result = await integration.config_entries.options.async_configure(result["flow_id"], {CONF_THRESHOLD_HEAT: 19.5})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    conf = merged_config(entry)
    assert conf[CONF_TARGET_VTHERM] == "vtherm-salon"
    assert conf[CONF_THRESHOLD_HEAT] == 19.5
    assert CONF_WEATHER_ENTITY not in conf
