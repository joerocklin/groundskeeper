"""Tests for the Mowing Director config flow."""

from homeassistant.config_entries import SOURCE_USER, ConfigEntryDisabler
from homeassistant.const import CONF_DEVICE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mowing_director.const import DOMAIN
from tests.conftest import make_mammotion_mower


async def test_flow_aborts_without_any_entry(hass) -> None:
    """No mammotion config entry at all -> abort no_supported_mower."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_supported_mower"


async def test_flow_aborts_with_only_rtk_station(hass) -> None:
    """A mammotion entry whose only device is the RTK station aborts too."""
    mammotion_entry = MockConfigEntry(
        domain="mammotion", title="Mammotion", unique_id="acct-1"
    )
    mammotion_entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    rtk = dev_reg.async_get_or_create(
        config_entry_id=mammotion_entry.entry_id,
        identifiers={("mammotion", "rtk-1")},
        name="RTK",
    )
    ent_reg.async_get_or_create(
        "switch", "mammotion", "rtk-1_power", config_entry=mammotion_entry, device_id=rtk.id
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_supported_mower"


async def test_flow_omits_already_configured_mower(hass) -> None:
    """A Mower with an existing entry is omitted; alone it aborts."""
    _, mower, _ = await make_mammotion_mower(hass)
    existing = MockConfigEntry(
        domain=DOMAIN, title="Example Mower", unique_id=mower.id, data={CONF_DEVICE: mower.id}
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_supported_mower"


async def test_flow_lists_only_unconfigured_mowers(hass) -> None:
    """With two Mowers, the picker offers only the one without an entry."""
    _, mower_a, _ = await make_mammotion_mower(hass, name="Luba-A", nick="First")
    _, mower_b, _ = await make_mammotion_mower(
        hass, name="Luba-B", nick="Second", with_rtk=False
    )
    existing = MockConfigEntry(
        domain=DOMAIN, title="First", unique_id=mower_a.id, data={CONF_DEVICE: mower_a.id}
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    options_field = result["data_schema"].schema[CONF_DEVICE]
    assert {opt["value"] for opt in options_field.config["options"]} == {mower_b.id}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: mower_b.id}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == mower_b.id
    assert result["result"].title == "Second"


async def test_flow_aborts_already_configured_on_stale_form(hass) -> None:
    """Submitting a Mower that gained an entry mid-flow aborts already_configured."""
    _, mower_a, _ = await make_mammotion_mower(hass, name="Luba-A", nick="First")
    _, mower_b, _ = await make_mammotion_mower(
        hass, name="Luba-B", nick="Second", with_rtk=False
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    # Another flow wins the race for mower A.
    other = MockConfigEntry(
        domain=DOMAIN, title="First", unique_id=mower_a.id, data={CONF_DEVICE: mower_a.id}
    )
    other.add_to_hass(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: mower_a.id}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_flow_lists_mower_and_creates_entry(hass, hass_storage) -> None:
    """Happy path: picker offers the Mower (not the RTK), confirm creates entry."""
    _, mower, rtk = await make_mammotion_mower(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    # The picker must offer the Mower and not the RTK station.
    schema = result["data_schema"]
    options_field = schema.schema[CONF_DEVICE]
    option_values = {opt["value"] for opt in options_field.config["options"]}
    assert option_values == {mower.id}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: mower.id}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    # The confirmation step carries the debug-recording disclosure.
    assert result["description_placeholders"] == {"mower": "Example Mower"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == mower.id
    assert entry.title == "Example Mower"
    assert entry.data == {CONF_DEVICE: mower.id}
    assert entry.options == {}

    # One Store per Mower, created with the right key and sections.
    key = f"mowing_director.{entry.entry_id}"
    assert key in hass_storage
    stored = hass_storage[key]
    assert stored["version"] == 1
    assert stored["minor_version"] == 0
    assert stored["key"] == key
    assert set(stored["data"]) == {"config", "state", "learned", "history"}
    assert stored["data"]["config"]["recurrences"] == []

    # No options flow, so HA shows no Configure button.
    assert not entry.supports_options

    # Unload and reload cleanly.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.value == "not_loaded"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.value == "loaded"
    assert not entry.supports_options

    # Disable and re-enable cleanly.
    await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert entry.disabled_by is not None
    assert entry.state.value == "not_loaded"
    await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done()
    assert entry.state.value == "loaded"

    # Removing the entry deletes the Store.
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
