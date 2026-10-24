"""A failed websocket call never renders as `[object Object]` (issue #62)."""

from __future__ import annotations

import json

import pytest

from tests.test_panel_zones import js  # noqa: F401  (also applies the node skip)


@pytest.mark.parametrize(
    ("err", "shown"),
    [
        ({"code": "not_found", "message": "Unknown Mower"}, "Unknown Mower"),
        ({"code": "invalid", "message": ""}, "invalid"),
        ({"code": "invalid"}, "invalid"),
        ({}, "Request failed"),
        ({"message": ""}, "Request failed"),
        (3, "Request failed"),
        ("boom", "boom"),
        (None, "Request failed"),
    ],
)
def test_error_message_is_always_human_text(err, shown) -> None:
    assert js(f"errMessage({json.dumps(err)})") == shown
