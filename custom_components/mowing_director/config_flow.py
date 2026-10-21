"""Config flow for Mowing Director (spec §4.1).

One step: a device picker listing only devices that have a `lawn_mower`
entity from an integration in the adapter registry. Already-configured
devices are omitted; with no eligible device the flow aborts with
`no_supported_mower`. A confirmation step discloses debug recording
before the entry is created.
"""

from __future__ import annotations

from typing import Any

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_DEVICE
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, selector

from .const import ADAPTER_DOMAINS, DOMAIN


class MowingDirectorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Mowing Director."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._device_id: str | None = None
        self._mower_name: str | None = None

    @callback
    def _mower_devices(self) -> list[dr.DeviceEntry]:
        """Devices with an enabled lawn_mower entity from an adapter integration.

        This is what leaves out the RTK station: it has no lawn_mower entity.
        """
        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)
        mowers: list[dr.DeviceEntry] = []
        for entry in self.hass.config_entries.async_entries():
            if entry.domain not in ADAPTER_DOMAINS:
                continue
            for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
                if device.disabled_by is not None:
                    continue
                if any(
                    reg_entry.domain == "lawn_mower"
                    and reg_entry.platform == entry.domain
                    for reg_entry in er.async_entries_for_device(ent_reg, device.id)
                ):
                    mowers.append(device)
        return mowers

    @callback
    def _picker_schema(self, devices: list[dr.DeviceEntry]) -> probatio.Schema:
        """Build the device picker as a select of the given device ids."""
        return probatio.Schema(
            {
                probatio.Required(CONF_DEVICE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=device.id,
                                label=device.name_by_user or device.name or device.id,
                            )
                            for device in devices
                        ]
                    )
                )
            }
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the Mower picker, then confirm the choice."""
        mowers = self._mower_devices()
        configured = self._async_current_ids()
        eligible = [device for device in mowers if device.id not in configured]
        if not eligible:
            return self.async_abort(reason="no_supported_mower")

        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=self._picker_schema(eligible)
            )

        device_id = user_input[CONF_DEVICE]
        if device_id not in {device.id for device in mowers}:
            # Stale form after the device list changed.
            return self.async_show_form(
                step_id="user",
                data_schema=self._picker_schema(eligible),
                errors={CONF_DEVICE: "no_supported_mower"},
            )

        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()

        device = dr.async_get(self.hass).async_get(device_id)
        if device is None:  # pragma: no cover - raced device removal
            return self.async_abort(reason="no_supported_mower")
        self._device_id = device_id
        self._mower_name = device.name_by_user or device.name or device_id

        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding the Mower, disclosing debug recording."""
        if user_input is not None:
            assert self._device_id is not None and self._mower_name is not None
            return self.async_create_entry(
                title=self._mower_name,
                data={CONF_DEVICE: self._device_id},
                description="mower_added",
                description_placeholders={
                    "mower": self._mower_name,
                    "panel_url": "/mowing_director",
                },
            )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"mower": self._mower_name},
        )
