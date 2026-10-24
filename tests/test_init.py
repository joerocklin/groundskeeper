"""Tests for Groundskeeper entry lifecycle and Store handling."""

from pathlib import Path

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_DEVICE
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.groundskeeper.const import DOMAIN
from tests.conftest import make_mammotion_mower


async def _create_entry(hass, mower):
    """Run the flow to create one Groundskeeper entry for mower."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: mower.id}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return result


async def test_create_entry_carries_panel_message(hass) -> None:
    """The create-entry result names the panel message with placeholders."""
    _, mower, _ = await make_mammotion_mower(hass)
    result = await _create_entry(hass, mower)

    assert result["description"] == "mower_added"
    assert result["description_placeholders"] == {
        "mower": "Example Mower",
        "panel_url": "/groundskeeper",
    }
    assert result["result"].data == {CONF_DEVICE: mower.id}


async def test_setup_reuses_existing_store_data(hass, hass_storage) -> None:
    """A Store that already has data is loaded, not overwritten."""
    _, mower, _ = await make_mammotion_mower(hass)
    entry = (await _create_entry(hass, mower))["result"]
    key = f"groundskeeper.{entry.entry_id}"

    hass_storage[key]["data"]["config"]["marker"] = "kept"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["config"]["marker"] == "kept"


async def test_remove_entry_deletes_store_and_backups(hass, hass_storage) -> None:
    """async_remove_entry deletes the Store file and our .bak migrations."""
    _, mower, _ = await make_mammotion_mower(hass)
    entry = (await _create_entry(hass, mower))["result"]
    key = f"groundskeeper.{entry.entry_id}"
    assert key in hass_storage

    # Real files on disk in the (tmp) config dir, as our migrations leave them.
    storage_dir = Path(hass.config.path(".storage"))
    storage_dir.mkdir(parents=True, exist_ok=True)
    bak = storage_dir / f"{key}.v1.bak"
    other_bak = storage_dir / "groundskeeper.other-entry.v1.bak"
    bak.write_text("{}")
    other_bak.write_text("{}")

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage
    assert not bak.exists()
    assert other_bak.exists()
    other_bak.unlink()
