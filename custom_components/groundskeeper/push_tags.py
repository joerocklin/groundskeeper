"""Push notification tags (#89).

The `tag` on a phone notification replaces or clears an earlier one. Home
Assistant's mobile_app drops a push whose tag is longer than 64 characters
(measured live: 64 arrived, 65 did not), and a tag built from a Mower's entry
id and a 26-character subject id ran to 65-74 characters. Every tag is built
here, so the bound holds in one place.
"""

from __future__ import annotations

import hashlib

_TAG_PREFIX = "mowdir_"
_READABLE_MAX = 12  # an id this short is kept as is: `wet_grass`, `g1`
_DIGEST_LEN = 10


def _short(value: str) -> str:
    """The id itself when it is short, else a stable digest of the whole id."""
    if len(value) <= _READABLE_MAX:
        return value
    return hashlib.sha256(value.encode()).hexdigest()[:_DIGEST_LEN]


def tag_prefix(entry_id: str) -> str:
    """What every tag of one Mower starts with; the `_` keeps `entry1` off `entry10`."""
    return f"{_TAG_PREFIX}{_short(entry_id)}_"


def notification_tag(entry_id: str, kind: str, subject: str | None = None) -> str:
    """The tag for one Prompt or notice: `mowdir_<mower>_<kind>[_<subject>]`.

    Stable (the same inputs give the same tag after an HA restart, so a later push
    replaces or clears an earlier one) and at most 64 characters, as long as `kind`
    is one of the short code constants it always is.
    """
    tag = f"{tag_prefix(entry_id)}{kind}"
    if subject:
        tag += f"_{_short(subject)}"
    return tag
