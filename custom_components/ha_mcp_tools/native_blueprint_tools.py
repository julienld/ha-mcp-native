"""Bind the single blueprint contract to its real Core domain owner."""

from typing import Any

from ha_mcp.runtime.blueprint_tools import BlueprintTools
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.blueprint_management import NativeBlueprints


def blueprint_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        tools = BlueprintTools(
            NativeBlueprints(
                manager.hass,
                actor,
                authorize=manager.backend.authorize,
                backup=manager.snapshots.blueprint_backup,
            )
        )
        return await tools.ha_manage_blueprints(**arguments)

    return define_tool(
        manager.backend,
        BlueprintTools,
        "ha_manage_blueprints",
        handler=invoke,
        source_module="tools_blueprints",
        tags=frozenset({"Blueprints"}),
        annotations=write_hints(
            "Manage Blueprints", destructive=True, idempotent=False, open_world=True
        ),
        meta={
            "ha_mcp": {
                "native_support": {
                    "note": "Uses Core's blueprint store, importer and consumer reloads. Backups retain installed YAML text; saving/restoring through Core normalizes YAML and does not preserve comments. File checks before dispatch are optimistic: Core does not offer an atomic compare-and-write against simultaneous external edits. Avoid editing the same file through another interface during a write or restore."
                }
            }
        },
    )
