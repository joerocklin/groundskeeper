"""Schedule controls shared by services and buttons (spec §6.3, §6.4, §13.2).

Each control checks that it applies to the Schedule's current state, raises a
translated `ServiceValidationError` if not (§13.1), then dispatches one input
to the reducer. Services and `button` entities both call these, so they can
never disagree about semantics.
"""

from __future__ import annotations

from homeassistant.const import ATTR_CONFIG_ENTRY_ID  # noqa: F401 - re-export for services
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .core import RunStatus, ScheduleStatus, active_run, next_run
from .runtime import Director


def fail(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


def _status(director: Director) -> str:
    return director.state["schedule"]["status"]


def _has_failed_run(director: Director) -> bool:
    return any(r["status"] == RunStatus.FAILED for r in director.state["runs"])


def _check_can_leave_paused(director: Director) -> None:
    if next_run(director.state) is None:
        raise fail("nothing_to_start")
    if _has_failed_run(director):
        raise fail("failure_needs_answer")


async def async_start(director: Director) -> None:
    if _status(director) not in (ScheduleStatus.IDLE, ScheduleStatus.PAUSED):
        raise fail("schedule_already_active")
    _check_can_leave_paused(director)
    await director.async_start()


async def async_resume(director: Director) -> None:
    if _status(director) != ScheduleStatus.PAUSED:
        raise fail("schedule_not_paused")
    _check_can_leave_paused(director)
    await director.async_resume()


async def async_pause(director: Director) -> None:
    if _status(director) not in (ScheduleStatus.WAITING, ScheduleStatus.RUNNING):
        raise fail("nothing_to_pause")
    await director.async_pause()


async def async_stop_now(director: Director) -> None:
    if active_run(director.state) is None:
        raise fail("no_active_run")
    await director.async_stop_now()


async def async_cancel(director: Director) -> None:
    if _status(director) == ScheduleStatus.IDLE and not director.state["runs"]:
        raise fail("nothing_to_cancel")
    await director.async_cancel()


async def async_retry_run(director: Director) -> None:
    if _status(director) != ScheduleStatus.PAUSED or not _has_failed_run(director):
        raise fail("no_failure_to_answer")
    await director.async_retry_run()


async def async_skip_plan(director: Director) -> None:
    if _status(director) != ScheduleStatus.PAUSED or not _has_failed_run(director):
        raise fail("no_failure_to_answer")
    await director.async_skip_plan()


def _require_on_hold(director: Director) -> None:
    run = active_run(director.state)
    if run is None or run["status"] != RunStatus.ON_HOLD:
        raise fail("no_hold_to_answer")


async def async_resume_run(director: Director) -> None:
    _require_on_hold(director)
    await director.async_resume_run()


async def async_end_run(director: Director) -> None:
    _require_on_hold(director)
    await director.async_end_run()


async def async_wet_grass_done_today(director: Director) -> None:
    """Call it a day for rain (spec §9.4): Wet grass fails until the next
    Work hours start plus the morning dry-out."""
    await director.async_wet_grass_done_today()


async def async_dismiss_anomaly(director: Director, detector: str) -> None:
    """Dismiss an active Anomaly (spec §11.7)."""
    if director.active_anomaly(detector) is None:
        raise fail("anomaly_not_active")
    await director.async_dismiss_anomaly(detector)
