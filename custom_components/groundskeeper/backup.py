"""HA backup platform: checkpoint the recording WAL, pause writes (spec §15.1)."""

from __future__ import annotations

from .recording import async_post_backup, async_pre_backup

__all__ = ["async_post_backup", "async_pre_backup"]
