"""Prompt content, built once for every channel (spec §12.7).

The phone notification, the panel card and the `persistent_notification`
all show the same `title` and `body`, rendered from translated strings.
Strings live under `exceptions` in `strings.json` because hassfest accepts no
custom top-level section; keys are prefixed `prompt_`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import translation

from .const import DOMAIN

_PREFIX = f"component.{DOMAIN}.exceptions."


async def async_prompt_strings(hass: HomeAssistant) -> dict[str, str]:
    """The `prompt_*` strings for the instance language (English fallback)."""
    found = await translation.async_get_translations(
        hass, hass.config.language, "exceptions", [DOMAIN]
    )
    return {
        key.removeprefix(_PREFIX).removesuffix(".message"): value
        for key, value in found.items()
        if key.startswith(_PREFIX + "prompt_")
    }


def _mm(value: Any) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def render_content(
    prompt: Mapping[str, Any], mower_name: str, strings: Mapping[str, str]
) -> dict[str, str]:
    """`{title, body}` for a Prompt: `<Mower name>: <headline>` and its lines."""
    ctx = prompt["context"]
    headline = strings.get(f"prompt_headline_{prompt['kind']}", prompt["kind"])
    lines: list[str] = []
    if "zone_name" in ctx:
        lines.append(strings["prompt_zone"].format(zone=ctx["zone_name"]))
    if "gate_name" in ctx:
        headline = headline.replace("{gate}", str(ctx["gate_name"]))
        lines.append(strings["prompt_gate"].format(gate=ctx["gate_name"]))
    if "plan_start_height_mm" in ctx:
        start, end = ctx["plan_start_height_mm"], ctx["plan_end_height_mm"]
        if start == end:
            lines.append(strings["prompt_plan_single"].format(height=_mm(start)))
        else:
            lines.append(
                strings["prompt_plan_range"].format(start=_mm(start), end=_mm(end))
            )
    if "n" in ctx:
        lines.append(
            strings["prompt_run"].format(
                n=ctx["n"], m=ctx["m"], height=_mm(ctx.get("height_mm"))
            )
        )
    if ctx.get("reason"):
        template = strings.get(f"prompt_reason_{ctx['reason']}")
        lines.append(
            template.format(height=_mm(ctx.get("height_mm")))
            if template
            else str(ctx["reason"])
        )
    return {"title": f"{mower_name}: {headline}", "body": "\n".join(lines)}
