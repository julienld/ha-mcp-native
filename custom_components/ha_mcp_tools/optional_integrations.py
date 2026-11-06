"""Resolve optional Core APIs only after their integration has been loaded.

Manifest after_dependencies install requirements even for unconfigured domains.
Optional radios, Supervisor, Assist and Cloud must instead borrow the active
installation. This boundary never sets up an integration or installs packages;
callers continue to pin and revalidate their runtime owners across awaits.
"""

from importlib import import_module
from types import ModuleType

from homeassistant.core import HomeAssistant

from ha_mcp.tool_api import ToolFailure


def loaded_integration_module(hass: HomeAssistant, module: str) -> ModuleType:
    """Resolve a relative Core module, refusing an inactive integration."""
    domain = module.partition(".")[0]
    if domain not in hass.config.components:
        raise ToolFailure(
            "SERVICE_UNAVAILABLE",
            f"Core integration {domain} is not loaded",
            status="not_started",
        )
    resolved = import_module(f"homeassistant.components.{module}")
    if domain not in hass.config.components:
        raise ToolFailure(
            "CONFIG_CONFLICT",
            f"Core integration {domain} unloaded during API resolution",
            status="not_started",
        )
    return resolved
