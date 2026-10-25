"""The Checks tab fetches on entry and after its own mutations, and builds the
all-entity candidate list lazily (issue #60), driven through node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_components.groundskeeper.panel import FRONTEND_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node missing")

SCRIPT = Path(__file__).with_name("panel_checks_smoke.js")
SRC = FRONTEND_DIR / "groundskeeper-panel.js"


@pytest.fixture(scope="module")
def out() -> dict:
    res = subprocess.run(
        ["node", str(SCRIPT), str(SRC)], check=True, capture_output=True, text=True
    )
    return json.loads(res.stdout)


def test_entering_the_tab_fetches_once(out) -> None:
    assert out["enter"] == 1


def test_snapshot_pushes_do_not_refetch(out) -> None:
    assert out["after_pushes"] == out["enter"]


def test_each_mutation_refetches_once(out) -> None:
    assert out["after_mutation"] == out["after_pushes"] + 1
    assert out["after_anomaly"] == out["after_mutation"] + 1


def test_render_has_no_all_entity_list_before_focus(out) -> None:
    assert out["render_entity_input"] is True
    assert out["render_all_options"] == 0


def test_focus_offers_every_entity_and_builds_once(out) -> None:
    for entity in ("switch.b", "weather.home", "sensor.a"):
        assert entity in out["focus_options"]
    assert out["built_once"] is True
    assert out["rerender_keeps_list"] == 1


def test_template_mode_never_builds_the_all_entity_list(out) -> None:
    assert out["template_has_all_list"] is False
