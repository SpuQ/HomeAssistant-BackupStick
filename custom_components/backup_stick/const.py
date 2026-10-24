"""Constants for the Backup Stick integration."""

from collections.abc import Callable

from homeassistant.util.hass_dict import HassKey

DOMAIN = "backup_stick"

CONF_UUID = "uuid"
CONF_FS_TYPE = "fs_type"

DATA_BACKUP_AGENT_LISTENERS: HassKey[list[Callable[[], None]]] = HassKey(f"{DOMAIN}.backup_agent_listeners")
