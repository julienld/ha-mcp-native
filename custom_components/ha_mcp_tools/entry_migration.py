"""Versioned config-entry migrations, run by Core before entry setup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    CONF_ENTRY_TYPE,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    ENTRY_TYPE_SERVER,
    OPT_PIP_SPEC,
)
from .llm_bridge_options import bridge_options

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Preserve effective pre-opt-in LLM choices without changing entry identity."""
    if entry.version != CONFIG_ENTRY_VERSION:
        return False
    if entry.minor_version < CONFIG_ENTRY_MINOR_VERSION:
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SERVER:
            # Upstream 1.2 only cleared old package overrides; export still
            # defaulted to True. Prototype 1.2 already saved explicit choices.
            # Never clear an override chosen after upstream's 1.2 migration.
            options = {
                key: value
                for key, value in entry.options.items()
                if entry.minor_version >= 2 or key != OPT_PIP_SPEC
            }
            hass.config_entries.async_update_entry(
                entry,
                options=bridge_options(options, export_default=True),
                minor_version=CONFIG_ENTRY_MINOR_VERSION,
            )
        else:
            hass.config_entries.async_update_entry(
                entry, minor_version=CONFIG_ENTRY_MINOR_VERSION
            )
    return True
