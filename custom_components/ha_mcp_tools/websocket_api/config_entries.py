"""WebSocket compatibility projection over Core config-entry readers."""

from typing import Any

from homeassistant.core import HomeAssistant

from ..core_config_entries import (
    _config_entry_row,
    load_entry_secret_values,
    project_config_entries,
)


async def _config_entries_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    return await load_entry_secret_values(hass)


def _do_config_entries(
    hass: HomeAssistant, params: dict[str, Any], **scrub: Any
) -> dict[str, Any]:
    return project_config_entries(hass, params, **scrub)


__all__ = [
    "_config_entry_row",
]
