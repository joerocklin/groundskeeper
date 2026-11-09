"""The setup checklist a new Mower's panel shows above every tab (spec §4.3).

Pure and HA-free: it reads the Mower's Store `config` and says which steps
are done. Only "Zones imported" is derived from the Zones themselves; the
rest follow from what the owner has set up or from a mark saved under
`config["setup"]`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# (id, label, tab the step is done in). `{mower}` is the Mower's name. The
# recording step has no tab: the banner states what is recorded and offers
# the controls inline.
_STEPS = (
    ("zones", "Zones imported", "zones"),
    ("bearings", "Review Zone bearings", "zones"),
    ("gates", "Does {mower} pass through any gates?", "zones"),
    ("notify", "Where should {mower} reach you?", "settings"),
    ("recording", "Review debug recording", None),
)


# Steps the owner marks done, and the `config["setup"]` key each one sets.
MARKS = {
    "bearings": "bearings_reviewed",
    "notify": "notify_panel_only",
    "recording": "recording_reviewed",
}


ANSWERS = ("yes", "no")


def apply_mark(config: dict[str, Any], step: str, answer: str | None = None) -> None:
    """Record that the owner has done `step`, in the Mower's `config`.

    The Gates step records the owner's yes/no `answer` instead of a plain mark.
    """
    setup = config.setdefault("setup", {})
    if step == "gates":
        setup["gates_answer"] = answer
    else:
        setup[MARKS[step]] = True


def build_checklist(config: Mapping[str, Any], title: str) -> dict[str, Any]:
    """Ordered steps (id, label, tab, done) and whether all are done."""
    marks = config.get("setup") or {}
    done = {
        "zones": bool(config.get("zones")),
        "bearings": bool(marks.get("bearings_reviewed")),
        "gates": marks.get("gates_answer") == "no" or bool(config.get("gates")),
        "notify": bool(config.get("notify_targets")) or bool(marks.get("notify_panel_only")),
        "recording": bool(marks.get("recording_reviewed")),
    }
    items = [
        {"id": step, "label": label.format(mower=title), "tab": tab, "done": done[step]}
        for step, label, tab in _STEPS
    ]
    for item in items:
        if item["id"] == "gates":  # "yes" leaves the step open until a Gate exists
            item["answer"] = marks.get("gates_answer")
    return {"items": items, "complete": all(i["done"] for i in items)}
