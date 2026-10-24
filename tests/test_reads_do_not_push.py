"""Read-only websocket commands must not push a snapshot.

The panel refetches on every snapshot push, so a read that pushes loops for as
long as its tab is open (issues #60, #67).
"""

import pytest
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.groundskeeper.websocket_api import SIGNAL_STATE_CHANGED
from tests.test_checks_ha import setup, ws  # noqa: F401

READS = [
    "get_zones", "get_zone_angles", "get_now", "list_prompts", "get_state",
    "list_checks", "get_schedule", "get_run_settings", "list_gates",
    "get_notify_settings", "get_retention", "history",
]


@pytest.mark.parametrize("command", READS)
async def test_read_does_not_push_a_snapshot(hass, hass_ws_client, setup, command) -> None:  # noqa: F811
    entry, _ = setup
    pushes = []
    async_dispatcher_connect(
        hass, SIGNAL_STATE_CHANGED.format(entry.entry_id), lambda: pushes.append(command))
    msg = await ws(hass, hass_ws_client, entry, command)
    await hass.async_block_till_done()
    assert msg["success"], msg
    assert pushes == []
