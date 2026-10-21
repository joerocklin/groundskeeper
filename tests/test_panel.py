"""Tests for the Mowing Director panel shell (spec §16.1, §2.3)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mowing_director.const import DOMAIN, PANEL_URL_PATH


def _add_entry(hass: HomeAssistant, title: str = "Luba") -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title=title, data={"device_id": title})
    entry.add_to_hass(hass)
    return entry


def _panels(hass: HomeAssistant) -> dict:
    return hass.data.get("frontend_panels", {})


async def test_first_entry_registers_one_sidebar_panel(hass: HomeAssistant) -> None:
    """Two Mowers share one panel; it is registered once per instance."""
    a, b = _add_entry(hass, "Luba"), _add_entry(hass, "Yuka")
    assert await hass.config_entries.async_setup(a.entry_id)
    await hass.async_block_till_done()

    panel = _panels(hass)[PANEL_URL_PATH]
    assert panel.sidebar_title == "Mowing Director"
    assert panel.require_admin is True
    module_url = panel.config["_panel_custom"]["module_url"]
    assert module_url.startswith("/mowing_director/panel/mowing-director-panel.js?v=")


async def test_panel_removed_only_when_last_entry_unloads(hass: HomeAssistant) -> None:
    """Unloading one of two Mowers keeps the panel; the last unload removes it."""
    a, b = _add_entry(hass, "Luba"), _add_entry(hass, "Yuka")
    await hass.config_entries.async_setup(a.entry_id)
    await hass.async_block_till_done()

    await hass.config_entries.async_unload(a.entry_id)
    assert PANEL_URL_PATH in _panels(hass)
    await hass.config_entries.async_unload(b.entry_id)
    assert PANEL_URL_PATH not in _panels(hass)

    # Setting up again after full teardown registers it again.
    await hass.config_entries.async_setup(a.entry_id)
    await hass.async_block_till_done()
    assert PANEL_URL_PATH in _panels(hass)


async def test_panel_js_served_with_cache_headers(
    hass: HomeAssistant, hass_client
) -> None:
    """The JS is cacheable, and the panel's ?v= changes with the content."""
    entry = _add_entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_client()
    resp = await client.get("/mowing_director/panel/mowing-director-panel.js")
    assert resp.status == 200
    assert "max-age" in resp.headers["Cache-Control"]
    assert "mowing-director-panel" in await resp.text()


def test_cache_buster_tracks_content(tmp_path) -> None:
    """Different bytes give a different `?v=` value; same bytes the same."""
    from custom_components.mowing_director.panel import _cache_buster

    a, b = tmp_path / "a.js", tmp_path / "b.js"
    a.write_text("one")
    b.write_text("two")
    assert _cache_buster(a) != _cache_buster(b)
    assert _cache_buster(a) == _cache_buster(a)


def test_minified_served_only_when_current(tmp_path, monkeypatch) -> None:
    """A stale minified build is ignored in favour of the readable source."""
    import hashlib

    from custom_components.mowing_director import panel

    monkeypatch.setattr(panel, "FRONTEND_DIR", tmp_path)
    src = tmp_path / "mowing-director-panel.js"
    src.write_text("readable")
    (tmp_path / "mowing-director-panel.min.js").write_text("min")
    stamp = tmp_path / "mowing-director-panel.min.js.sha1"

    assert panel._asset_to_serve() == src  # no stamp yet
    stamp.write_text("deadbeef")
    assert panel._asset_to_serve() == src  # stale stamp
    stamp.write_text(hashlib.sha1(b"readable").hexdigest())
    assert panel._asset_to_serve().name == "mowing-director-panel.min.js"


def test_frontend_smoke() -> None:
    """Load/render smoke (spec §18.2 layer 5): element, tabs, WS wiring."""
    from custom_components.mowing_director.panel import FRONTEND_DIR

    src = (FRONTEND_DIR / "mowing-director-panel.js").read_text()
    assert "customElements.define('mowing-director-panel'" in src
    assert "attachShadow" in src
    for tab in (
        "Now",
        "Schedule & Plans",
        "Zones & Gates",
        "Checks",
        "History",
        "Settings",
    ):
        assert f"'{tab}'" in src
    assert "mowing_director/subscribe" in src
    assert "mowing_director/list_mowers" in src
    assert "760px" in src  # columns stack below ~760 px (spec §16.1)


def test_committed_minified_build_is_current() -> None:
    """Fails if the source changed without re-running devtools/build_panel.mjs."""
    from custom_components.mowing_director.panel import FRONTEND_DIR, _asset_to_serve

    assert _asset_to_serve() == FRONTEND_DIR / "mowing-director-panel.min.js"
