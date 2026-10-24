"""Config flow: pick one of the USB sticks plugged into this machine."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import CONF_FS_TYPE, CONF_UUID, DOMAIN
from .stick import find_sticks


class BackupStickConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        sticks = await self.hass.async_add_executor_job(find_sticks)
        configured = {entry.unique_id for entry in self._async_current_entries()}
        available = {stick.uuid: stick for stick in sticks if stick.uuid not in configured}
        if not available:
            return self.async_abort(reason="no_sticks")

        if user_input is not None:
            stick = available[user_input[CONF_UUID]]
            await self.async_set_unique_id(stick.uuid)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"USB stick {stick.title}",
                data={CONF_UUID: stick.uuid, CONF_FS_TYPE: stick.fs_type},
            )

        options = [
            SelectOptionDict(value=stick.uuid, label=f"{stick.title} ({stick.fs_type}, {stick.size / 1e9:.1f} GB)")
            for stick in available.values()
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_UUID): SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST))}
            ),
        )
