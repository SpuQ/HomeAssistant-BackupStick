"""
Backup Stick: a USB stick plugged into the Home Assistant machine as a backup location.

Each configured stick shows up under Settings → System → Backups as a location
of its own, next to the backups kept on the machine itself.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import CONF_FS_TYPE, CONF_UUID, DATA_BACKUP_AGENT_LISTENERS
from .stick import BackupStick

type BackupStickConfigEntry = ConfigEntry[BackupStick]


async def async_setup_entry(hass: HomeAssistant, entry: BackupStickConfigEntry) -> bool:
    # Nothing is mounted here: the stick is mounted only while a backup is
    # written, read or listed, and it may well be unplugged right now.
    entry.runtime_data = BackupStick(entry.data[CONF_UUID], entry.data[CONF_FS_TYPE])

    @callback
    def notify_backup_listeners() -> None:
        for listener in hass.data.get(DATA_BACKUP_AGENT_LISTENERS, []):
            listener()

    entry.async_on_unload(entry.async_on_state_change(notify_backup_listeners))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BackupStickConfigEntry) -> bool:
    return True
