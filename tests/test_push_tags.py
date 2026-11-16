"""Push notification tags (#89).

Home Assistant's mobile_app drops a push whose `tag` is longer than 64
characters: measured live on 2026-11-16, a 64-character tag arrived and a
65-character one did not. The ids below are real length (entry, Gate and Run
ids are 26-character ULIDs), which the fixtures with short ids like "entry1"
never exercised.
"""

import pytest

from custom_components.groundskeeper.core import (
    PROMPT_ANOMALY,
    PROMPT_GATE,
    PROMPT_GATE_CLOSE,
    PROMPT_IS_RAINING,
    PROMPT_RUN_FAILED,
    PROMPT_RUN_ON_HOLD,
)
from custom_components.groundskeeper.push_tags import notification_tag, tag_prefix

ENTRY = "01M5QSXDGAFZ5EK0SXRFJEW2D2"
GATE = "01M5QSXDJJG7AWR520WCC3Z314"

PROMPT_KINDS = [
    PROMPT_RUN_FAILED,
    PROMPT_RUN_ON_HOLD,
    PROMPT_IS_RAINING,
    PROMPT_GATE,
    PROMPT_GATE_CLOSE,
    PROMPT_ANOMALY,
]
# The plain notices the Notifier sends (`async_notice` callers).
NOTICE_KINDS = ["run_started", "run_done", "schedule_completed", "check_timeout"]


@pytest.mark.parametrize("subject", [None, GATE])
@pytest.mark.parametrize("kind", PROMPT_KINDS + NOTICE_KINDS)
def test_every_tag_fits_the_push_limit_with_real_length_ids(kind, subject) -> None:
    assert len(notification_tag(ENTRY, kind, subject)) <= 64


def test_the_prefix_claims_one_mowers_tags_and_no_other() -> None:
    """A tapped action finds its Mower by this prefix when the Prompt is gone."""
    other = ENTRY[:-1] + "3"
    mine = [notification_tag(ENTRY, kind, GATE) for kind in PROMPT_KINDS + NOTICE_KINDS]
    assert all(tag.startswith(tag_prefix(ENTRY)) for tag in mine)
    assert not any(
        notification_tag(other, kind, GATE).startswith(tag_prefix(ENTRY))
        for kind in PROMPT_KINDS + NOTICE_KINDS
    )
    # a short id must not claim the tags of a longer id that starts with it
    assert not notification_tag("entry10", "gate", "g1").startswith(tag_prefix("entry1"))


def test_a_tag_is_the_same_after_a_restart() -> None:
    """A later push can only replace or clear an earlier one if the tag repeats,
    including across an HA restart (so no salted `hash()`). Pinned literal."""
    assert notification_tag(ENTRY, "gate", GATE) == "mowdir_daf5a276d8_gate_37757d6533"


def test_ids_that_differ_only_at_the_end_get_different_tags() -> None:
    """Ids made in the same millisecond share all but their random tail."""
    other_gate = GATE[:-1] + "5"
    other_entry = ENTRY[:-1] + "3"
    base = notification_tag(ENTRY, "gate", GATE)
    assert notification_tag(ENTRY, "gate", other_gate) != base
    assert notification_tag(other_entry, "gate", GATE) != base


def test_ids_that_already_fit_stay_readable() -> None:
    """Check ids like `wet_grass` and the short ids of test fixtures are not hashed."""
    assert notification_tag("entry1", "run_failed") == "mowdir_entry1_run_failed"
    assert notification_tag("entry1", "gate", "g1") == "mowdir_entry1_gate_g1"
    assert (
        notification_tag("entry1", "check_timeout", "wet_grass")
        == "mowdir_entry1_check_timeout_wet_grass"
    )
