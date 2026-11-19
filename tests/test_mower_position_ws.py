"""Mower position on the Now tab's map: the websocket subscription (issue #113).

Fakes only (`FakeMower`, `hass_ws_client`); nothing here calls a live Mower.
The position is read on demand and pushed to the panel, and must never reach
the Snapshot, the Store, diagnostics or anything the integration emits.
"""

import json
from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.groundskeeper.adapter import MowerPosition
from custom_components.groundskeeper.diagnostics import async_get_config_entry_diagnostics
from custom_components.groundskeeper.websocket_api import POSITION_PUSH_INTERVAL
from tests.mammotion_states import install_baseline
from tests.test_mammotion_adapter import NAME, add_tracker, make_adapter, make_director_entry
from tests.test_tracer_bullet import MOWING, advance, director  # noqa: F401

LAT, LON = 39.395642778825284, -84.45706392229717


def fix(freezer, *, lat=LAT, lon=LON, direction=276, age_s=0) -> MowerPosition:
    return MowerPosition(
        latitude=lat,
        longitude=lon,
        fixed_at=datetime.now(UTC) - timedelta(seconds=age_s),
        direction_deg=direction,
    )


async def subscribe(hass, hass_ws_client, entry, sub_id=5):
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": sub_id, "type": "groundskeeper/subscribe_mower_position", "entry_id": entry.entry_id}
    )
    return client


async def pushes(client, count):
    """The next `count` event payloads (after the subscription's result)."""
    out = []
    while len(out) < count:
        msg = await client.receive_json()
        if msg["type"] == "event":
            out.append(msg["event"])
    return out


async def drain(hass, client, marker_id=99):
    """Every event queued so far, found by waiting for a get_now reply."""
    await hass.async_block_till_done()
    await client.send_json(
        {"id": marker_id, "type": "groundskeeper/get_now", "entry_id": "unused"}
    )
    seen = []
    while (msg := await client.receive_json())["id"] != marker_id:
        if msg["type"] == "event":
            seen.append(msg["event"])
    return seen


async def tick(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# -- the first push --------------------------------------------------------------------


async def test_subscribing_answers_then_pushes_the_position_at_once(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    mower.mower_position_value = fix(freezer, age_s=12)
    client = await subscribe(hass, hass_ws_client, entry)

    result = await client.receive_json()
    assert result["id"] == 5 and result["success"]
    [first] = await pushes(client, 1)
    assert first == {
        "position": {"latitude": LAT, "longitude": LON, "direction": 276, "age_s": 12.0}
    }


async def test_the_fix_age_is_a_duration_not_a_clock_reading(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    mower.mower_position_value = fix(freezer, age_s=3.5)
    client = await subscribe(hass, hass_ws_client, entry)
    await client.receive_json()
    [first] = await pushes(client, 1)
    assert first["position"]["age_s"] == 3.5
    assert "fixed_at" not in json.dumps(first)


async def test_a_fix_without_direction_is_pushed_without_one(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    mower.mower_position_value = fix(freezer, direction=None)
    client = await subscribe(hass, hass_ws_client, entry)
    await client.receive_json()
    [first] = await pushes(client, 1)
    assert first["position"]["direction"] is None
    assert first["position"]["latitude"] == LAT


async def test_no_fix_is_a_null_position_not_an_error(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    mower.mower_position_value = None
    client = await subscribe(hass, hass_ws_client, entry)
    assert (await client.receive_json())["success"]
    assert await pushes(client, 1) == [{"position": None}]


async def test_an_adapter_without_the_capability_yields_no_position_and_no_error(
    hass, hass_ws_client, director
) -> None:
    entry, mower, _ = director
    mower.mower_position = None  # as if the adapter never offered the member
    client = await subscribe(hass, hass_ws_client, entry)
    assert (await client.receive_json())["success"]
    assert await pushes(client, 1) == [{"position": None}]


async def test_an_adapter_that_fails_reading_yields_no_position_and_no_error(
    hass, hass_ws_client, director
) -> None:
    entry, mower, _ = director
    mower.mower_position_error = RuntimeError("another integration's data broke")
    client = await subscribe(hass, hass_ws_client, entry)
    assert (await client.receive_json())["success"]
    assert await pushes(client, 1) == [{"position": None}]


# -- access --------------------------------------------------------------------------


async def test_an_unknown_entry_is_not_found(hass, hass_ws_client, director) -> None:
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "groundskeeper/subscribe_mower_position", "entry_id": "nope"}
    )
    msg = await client.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "not_found"


async def test_the_subscription_is_admin_only(
    hass, hass_ws_client, director, hass_read_only_access_token
) -> None:
    entry, mower, _ = director
    reads = []
    mower.mower_position = lambda: reads.append(1)  # must never be read for a non-admin
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json(
        {"id": 1, "type": "groundskeeper/subscribe_mower_position", "entry_id": entry.entry_id}
    )
    msg = await client.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "unauthorized"
    assert reads == []


# -- throttle --------------------------------------------------------------------------


async def test_pushes_follow_changes_at_most_once_per_interval(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    mower.mower_position_value = fix(freezer, lat=39.0)
    client = await subscribe(hass, hass_ws_client, entry)
    await client.receive_json()
    assert len(await pushes(client, 1)) == 1  # the immediate one

    # Many fixes arrive inside one interval: still nothing until it elapses...
    for i in range(1, 6):
        mower.mower_position_value = fix(freezer, lat=39.0 + i / 1000)
        await hass.async_block_till_done()
    assert await drain(hass, client) == []

    # ...and then exactly one push, carrying the latest fix.
    await tick(hass, freezer, POSITION_PUSH_INTERVAL.total_seconds())
    [latest] = await drain(hass, client)
    assert latest["position"]["latitude"] == 39.005


async def test_an_unchanged_fix_is_not_pushed_again(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    mower.mower_position_value = fix(freezer)
    client = await subscribe(hass, hass_ws_client, entry)
    await client.receive_json()
    await pushes(client, 1)

    for _ in range(4):
        await tick(hass, freezer, POSITION_PUSH_INTERVAL.total_seconds())
    assert await drain(hass, client) == []


async def test_the_position_going_away_is_pushed_once(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    mower.mower_position_value = fix(freezer)
    client = await subscribe(hass, hass_ws_client, entry)
    await client.receive_json()
    await pushes(client, 1)

    mower.mower_position_value = None
    await tick(hass, freezer, POSITION_PUSH_INTERVAL.total_seconds())
    await tick(hass, freezer, POSITION_PUSH_INTERVAL.total_seconds())
    assert await drain(hass, client) == [{"position": None}]


# -- teardown ----------------------------------------------------------------------------


def count_reads(mower, fixture):
    reads = []

    def read():
        reads.append(1)
        return fixture

    mower.mower_position = read
    return reads


async def test_nothing_is_read_after_the_subscription_is_cancelled(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    reads = count_reads(mower, fix(freezer))
    client = await subscribe(hass, hass_ws_client, entry)
    await client.receive_json()
    await pushes(client, 1)
    assert len(reads) == 1

    await tick(hass, freezer, POSITION_PUSH_INTERVAL.total_seconds())
    assert len(reads) == 2  # open: read on each tick

    await client.send_json({"id": 6, "type": "unsubscribe_events", "subscription": 5})
    assert (await client.receive_json())["success"]
    for _ in range(3):
        await tick(hass, freezer, POSITION_PUSH_INTERVAL.total_seconds())
    assert len(reads) == 2


async def test_nothing_is_read_after_the_connection_closes(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    reads = count_reads(mower, fix(freezer))
    client = await subscribe(hass, hass_ws_client, entry)
    await client.receive_json()
    await pushes(client, 1)

    await client.close()
    await hass.async_block_till_done()
    before = len(reads)
    for _ in range(3):
        await tick(hass, freezer, POSITION_PUSH_INTERVAL.total_seconds())
    assert len(reads) == before


async def test_the_position_is_not_read_without_a_subscription(
    hass, director, freezer
) -> None:
    """Merely running (a Run, snapshots, Now-tab pushes) never reads the position."""
    entry, mower, _ = director
    reads = count_reads(mower, fix(freezer))
    mower.emit_snapshot(MOWING)
    await advance(hass, freezer, 120)
    assert reads == []


# -- it is only shown ----------------------------------------------------------------------


async def test_the_position_never_enters_the_snapshot_the_store_or_the_panels_state(
    hass, hass_ws_client, hass_storage, director, freezer
) -> None:
    entry, mower, _ = director
    mower.mower_position_value = fix(freezer)
    client = await subscribe(hass, hass_ws_client, entry)
    await client.receive_json()
    await pushes(client, 1)
    mower.emit_snapshot(MOWING)
    await advance(hass, freezer, 120)
    await hass.async_block_till_done()

    assert mower.snapshot.movement.position is None
    persisted = json.dumps(hass_storage, default=str)
    assert "latitude" not in persisted and "39.3956" not in persisted and "84.457" not in persisted
    # The other pushes the panel gets carry none of it either.
    other = await drain(hass, client)
    assert "39.3956" not in json.dumps(other) and "84.457" not in json.dumps(other)
    for command in ("get_state", "get_now"):
        await client.send_json({"id": 50, "type": f"groundskeeper/{command}", "entry_id": entry.entry_id})
        reply = await client.receive_json()
        assert "39.3956" not in json.dumps(reply) and "84.457" not in json.dumps(reply)


async def test_diagnostics_never_carry_the_mower_position(hass: HomeAssistant) -> None:
    """With a real Mammotion tracker reporting GPS, the diagnostics download
    still holds no coordinates (raw GPS in diagnostics is #49's concern)."""
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)
    mammotion_entry = hass.config_entries.async_entries("mammotion")[0]
    add_tracker(hass, mammotion_entry, mower)
    assert make_adapter(hass, entry).mower_position() is not None  # the tracker is there

    text = json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)
    assert "39.3956" not in text and "84.457" not in text
    assert "latitude" not in text and "longitude" not in text
