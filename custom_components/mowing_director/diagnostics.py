"""Diagnostics download for one Mower (issue #8, spec §15).

Snapshot + limits + version metadata with the device serial redacted.
Debug recording itself is a later ticket; #8 emits the read-only view.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .adapter import Limits, MowerAdapter
from .adapters import get_adapter_factory


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, object]:
    """Normalised Mower view for the diagnostics download, serial redacted."""
    adapter = _adapter_for(hass, entry)
    if adapter is None:
        return {"error": "no adapter for this Mower"}
    device = dr.async_get(hass).async_get(entry.data.get(CONF_DEVICE, ""))

    await adapter.async_start()
    try:
        data: dict[str, object] = {
            "snapshot": asdict(adapter.snapshot),
            "limits": _limits_payload(adapter.limits),
            "metadata": {
                "source_domain": adapter.source_domain,
                "source_version": adapter.source_version,
                "model": adapter.model,
                "firmware_version": adapter.firmware_version,
                # The Mammotion device name carries the serial; it is here
                # so support can match a report to a unit only after the
                # serial is masked out.
                "device_name": device.name if device else None,
            },
        }
    finally:
        await adapter.async_close()

    return cast(dict[str, object], _redact(data, adapter.redact))


def _adapter_for(hass: HomeAssistant, entry: ConfigEntry) -> MowerAdapter | None:
    """Build the adapter for the entry's Mower device, or None if unsupported."""
    device = dr.async_get(hass).async_get(entry.data.get(CONF_DEVICE, ""))
    if device is None:
        return None
    for domain, _identifier in device.identifiers:
        factory = get_adapter_factory(domain)
        if factory is not None:
            return factory(hass, entry)
    return None


def _limits_payload(limits: Limits) -> dict[str, object]:
    """Limits as JSON-ready data; selectors serialise like the panel sees."""
    payload = asdict(limits)
    payload["extras_schema"] = [
        {
            "key": field["key"],
            "label": field["label"],
            "selector": field["selector"].serialize(),
            "default": field["default"],
        }
        for field in payload["extras_schema"]
    ]
    return payload


def _redact(data: object, terms: list[str]) -> object:
    """Replace every serial-bearing term inside string values.

    `homeassistant.helpers.redact.async_redact_data` masks by key; the
    serial also appears inside values (device names, entity ids), so the
    adapter's term list drives a substring pass instead.
    """
    if isinstance(data, str):
        for term in terms:
            if term:
                data = data.replace(term, "**REDACTED**")
        return data
    if isinstance(data, dict):
        return {key: _redact(value, terms) for key, value in data.items()}
    if isinstance(data, list):
        return [_redact(value, terms) for value in data]
    return data
