"""Bind the entity-removal public contract to its canonical Core owner."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.entity_removal_tools import EntityRemovalTools
from ha_mcp.runtime.entity_write_tools import EntityWriteTools
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.entity_registry import NativeEntityRegistry
from .native_operations.entity_writes import NativeEntityWrites


def entity_removal_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        service = NativeEntityRegistry(
            manager.hass,
            actor,
            authorize=manager.backend.authorize,
            backup=manager.snapshots.registry_backup,
        )
        return await EntityRemovalTools(service).ha_remove_entity(**arguments)

    return define_tool(
        manager.backend,
        EntityRemovalTools,
        "ha_remove_entity",
        source_module="tools_entities",
        tags=frozenset({"Entity Registry"}),
        handler=invoke,
        annotations=write_hints(
            "Remove Entity", destructive=True, idempotent=True, open_world=False
        ),
    )


def entity_write_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        service = NativeEntityWrites(
            manager.hass,
            actor,
            authorize=manager.backend.authorize,
            backup=manager.snapshots.registry_backup,
        )
        return await EntityWriteTools(service).ha_set_entity(**arguments)

    return define_tool(
        manager.backend,
        EntityWriteTools,
        "ha_set_entity",
        source_module="tools_entities",
        tags=frozenset({"Entity Registry"}),
        handler=invoke,
        annotations=write_hints(
            "Set Entity", destructive=True, idempotent=True, open_world=False
        ),
    )
