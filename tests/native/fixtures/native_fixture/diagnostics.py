"""Exercise the actual diagnostics platform and Core artifact formatter."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict[str, Any]:
    return {
        "entry_id": config_entry.entry_id,
        "values": [{"id": 1}, {"id": 2}, {"id": 3}],
    }
