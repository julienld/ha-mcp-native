"""Versioned config-entry migrations, run by Core before entry setup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    CONF_ENTRY_TYPE,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    ENTRY_TYPE_SERVER,
)
from .llm_bridge_options import bridge_options

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Preserve effective pre-opt-in LLM choices without changing entry identity."""
    if entry.version != CONFIG_ENTRY_VERSION:
        return False
    if entry.minor_version < 2:
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SERVER:
            # Upstream 1.1 entries omitted this option while export defaulted
            # to True. Only migration uses that default; new entries start off.
            hass.config_entries.async_update_entry(
                entry,
                options=bridge_options(entry.options, export_default=True),
                minor_version=CONFIG_ENTRY_MINOR_VERSION,
            )
        else:
            hass.config_entries.async_update_entry(
                entry, minor_version=CONFIG_ENTRY_MINOR_VERSION
            )
    return True
