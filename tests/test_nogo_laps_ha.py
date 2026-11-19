"""No-go laps on a Plan (#109): the panel's New Plan path through the backend.

The adapter's own extra key never appears here: the setting is found through
the descriptor the views carry, the way the panel finds it.
"""

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_DEVICE
from homeassistant.exceptions import HomeAssistantError

from custom_components.groundskeeper import adapters
from custom_components.groundskeeper.const import DOMAIN
from custom_components.groundskeeper.panel import FRONTEND_DIR
from tests.conftest import make_mammotion_mower
from tests.fake_mower import NOGO_LIMITS, FakeMower
from tests.test_checks_ha import ws

PLAN = {"start_height_mm": 70, "end_height_mm": 60}


@pytest.fixture
async def setup(hass, monkeypatch):
    """A set-up entry whose adapter declares a No-go laps extra (see NOGO_LIMITS)."""
    mower = FakeMower(limits_descriptor=NOGO_LIMITS)
    monkeypatch.setitem(adapters.ADAPTER_REGISTRY, "mammotion", lambda h, e: mower)
    _, device, _ = await make_mammotion_mower(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: device.id})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    return result["result"], mower


async def zones(hass, hass_ws_client, entry):
    return (await ws(hass, hass_ws_client, entry, "get_zones"))["result"]


async def descriptor(hass, hass_ws_client, entry):
    return (await zones(hass, hass_ws_client, entry))["nogo_laps"]


async def zone_id(hass, hass_ws_client, entry, name):
    view = await zones(hass, hass_ws_client, entry)
    return next(z["id"] for z in view["zones"] if z["name"] == name)


async def test_the_zone_view_describes_the_no_go_laps_setting(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    view = await zones(hass, hass_ws_client, entry)
    d = view["nogo_laps"]
    assert d["min"] == 0 and d["max"] == 4 and d["key"]
    front = next(z for z in view["zones"] if z["name"] == "Front Lawn")
    assert front["nogo_laps_default"] == 0  # the resolved default, for the placeholder


async def test_a_zone_default_shows_as_that_zones_resolved_default(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    key = (await descriptor(hass, hass_ws_client, entry))["key"]
    front = await zone_id(hass, hass_ws_client, entry, "Front Lawn")
    msg = await ws(hass, hass_ws_client, entry, "set_zone_run_settings",
                   zone_id=front, run_defaults={"extras": {key: 2}})
    assert msg["success"]
    view = await zones(hass, hass_ws_client, entry)
    assert next(z for z in view["zones"] if z["id"] == front)["nogo_laps_default"] == 2


@pytest.mark.parametrize("type_", ["preview_plan", "add_plan"])
@pytest.mark.parametrize("bad", [5, -1])
async def test_a_value_outside_the_range_fails_preview_and_add_and_makes_no_plan(
    hass, hass_ws_client, setup, type_, bad
) -> None:
    entry, _ = setup
    key = (await descriptor(hass, hass_ws_client, entry))["key"]
    msg = await ws(hass, hass_ws_client, entry, type_, zone="Front Lawn",
                   settings={"extras": {key: bad}}, **PLAN)
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_settings"
    assert "outside the allowed range" in msg["error"]["message"]
    schedule = (await ws(hass, hass_ws_client, entry, "get_schedule"))["result"]
    assert schedule["plans"] == []


async def test_a_zone_without_no_go_areas_still_accepts_the_setting(
    hass, hass_ws_client, setup
) -> None:
    """The backend stays permissive; the Mower ignores the laps there (#109)."""
    entry, _ = setup
    key = (await descriptor(hass, hass_ws_client, entry))["key"]
    msg = await ws(hass, hass_ws_client, entry, "add_plan", zone="Backyard",
                   settings={"extras": {key: 2}}, **PLAN)
    assert msg["success"], msg


async def test_a_value_is_stored_on_every_run_including_the_finishing_run(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    key = (await descriptor(hass, hass_ws_client, entry))["key"]
    msg = await ws(hass, hass_ws_client, entry, "add_plan", zone="Front Lawn",
                   finishing=True, settings={"extras": {key: 3}}, **PLAN)
    assert msg["success"], msg
    runs = (await ws(hass, hass_ws_client, entry, "get_schedule"))["result"]["runs"]
    assert len(runs) >= 2
    assert [r["settings"]["extras"][key] for r in runs] == [3] * len(runs)


async def test_a_blank_value_leaves_the_plan_as_it_is_today(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    key = (await descriptor(hass, hass_ws_client, entry))["key"]
    msg = await ws(hass, hass_ws_client, entry, "add_plan", zone="Front Lawn", **PLAN)
    assert msg["success"]
    schedule = (await ws(hass, hass_ws_client, entry, "get_schedule"))["result"]
    assert schedule["plans"][0]["settings"] == {}
    assert {r["settings"]["extras"][key] for r in schedule["runs"]} == {0}


async def test_the_schedule_view_names_the_no_go_laps_key_the_cards_read(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    key = (await descriptor(hass, hass_ws_client, entry))["key"]
    schedule = (await ws(hass, hass_ws_client, entry, "get_schedule"))["result"]
    assert schedule["nogo_laps_key"] == key


async def test_the_add_plan_service_rejects_an_out_of_range_value_too(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    key = (await descriptor(hass, hass_ws_client, entry))["key"]
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "add_plan",
            {"zone": "front lawn", "settings": {"extras": {key: 5}}, **PLAN},
            blocking=True, return_response=True)
    schedule = (await ws(hass, hass_ws_client, entry, "get_schedule"))["result"]
    assert schedule["plans"] == []


def test_neither_core_nor_panel_names_the_mammotion_key() -> None:
    """The setting is brand-neutral outside the adapter (#109)."""
    root = FRONTEND_DIR.parent
    sources = [p for p in root.rglob("*.py") if "adapters" not in p.parts]
    sources += [p for p in FRONTEND_DIR.glob("*.js")]
    assert sources
    offenders = [p.name for p in sources if "obstacle_laps" in p.read_text()]
    assert offenders == []
