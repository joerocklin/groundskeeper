"""Sidebar panel registration (spec §2.3, §16.1).

The panel is registered once per HA instance, by the first loaded entry, and
removed when the last entry unloads. The static JS is served with
`cache_headers=True` (31-day cache), so the module URL carries a `?v=` content
hash to bust it on every change.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    PANEL_ELEMENT,
    PANEL_JS,
    PANEL_JS_MIN,
    PANEL_STATIC_URL,
    PANEL_URL_PATH,
)

FRONTEND_DIR = Path(__file__).parent / "frontend"
_STATIC_KEY = f"{DOMAIN}_panel_static"
_LOCK_KEY = f"{DOMAIN}_panel_lock"


def _asset_to_serve() -> Path:
    """Serve the minified build when it is current, else the readable source.

    Both are committed (HACS ships the repo as-is). The minified file is
    trusted only if a sibling `.sha1` records the source's current hash.
    """
    source = FRONTEND_DIR / PANEL_JS
    minified = FRONTEND_DIR / PANEL_JS_MIN
    stamp = FRONTEND_DIR / f"{PANEL_JS_MIN}.sha1"
    if minified.is_file() and stamp.is_file():
        if stamp.read_text().strip() == hashlib.sha1(source.read_bytes()).hexdigest():
            return minified
    return source


def _cache_buster(asset: Path) -> str:
    return hashlib.sha1(asset.read_bytes()).hexdigest()[:10]


def _read_asset_info() -> tuple[Path, str]:
    asset = _asset_to_serve()
    return asset, _cache_buster(asset)


async def async_register_panel(hass: HomeAssistant) -> None:
    """Register the static JS and the sidebar panel, once per instance."""
    # Entries set up concurrently; serialise so only one registers.
    lock = hass.data.setdefault(_LOCK_KEY, asyncio.Lock())
    async with lock:
        await _async_register_panel_locked(hass)


async def _async_register_panel_locked(hass: HomeAssistant) -> None:
    if frontend.async_panel_exists(hass, PANEL_URL_PATH):
        return
    asset, version = await hass.async_add_executor_job(_read_asset_info)
    if not hass.data.get(_STATIC_KEY):
        # HA cannot unregister a static path, so register it only once.
        await hass.http.async_register_static_paths(
            [StaticPathConfig(f"{PANEL_STATIC_URL}/{PANEL_JS}", str(asset), True)]
        )
        hass.data[_STATIC_KEY] = True
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title="Groundskeeper",
        sidebar_icon="mdi:robot-mower",
        module_url=f"{PANEL_STATIC_URL}/{PANEL_JS}?v={version}",
        embed_iframe=False,
        require_admin=True,
        config_panel_domain=DOMAIN,
        handle_safe_area=True,
    )


def async_remove_panel(hass: HomeAssistant) -> None:
    """Remove the sidebar panel (the last entry unloaded)."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
