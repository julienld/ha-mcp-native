"""Bind the shared Energy signature to its direct Core preference owner."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.energy_tools import EnergyFacade
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.energy import NativeEnergy
from .native_operations.registry_access import registry_failure


def energy_definition(manager: Any) -> ToolDefinition:
    async def manage(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        facade = EnergyFacade()
        facade.energy = NativeEnergy(
            manager.hass, actor, authorize=manager.backend.authorize
        )
        try:
            return await facade.ha_manage_energy_prefs(**arguments)
        except Exception as error:
            raise registry_failure(
                error, {"operation": "energy_preferences"}
            ) from error

    return define_tool(
        manager.backend,
        EnergyFacade,
        "ha_manage_energy_prefs",
        handler=manage,
        source_module="tools_energy",
        tags=frozenset({"Energy"}),
        annotations=write_hints(
            "Manage Energy Dashboard Preferences",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
        writes=lambda arguments: (
            arguments["mode"] != "get" and not arguments["dry_run"]
        ),
    )
