"""Bind update schemas and effects to the canonical Core service."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.update_tools import UpdateToolsFacade
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolResult
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.updates import NativeUpdates


def update_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> Any:
        result = await UpdateToolsFacade(
            NativeUpdates(manager, actor)
        ).ha_manage_updates(**arguments)
        status = result.get("operation_outcome")
        return ToolResult(result, status=status) if status is not None else result

    return define_tool(
        manager.backend,
        UpdateToolsFacade,
        "ha_manage_updates",
        handler=invoke,
        source_module="tools_updates",
        tags=frozenset({"System"}),
        annotations=write_hints(
            "Manage Updates", destructive=True, idempotent=False, open_world=True
        ),
        writes=lambda arguments: arguments.get("action", "list") not in {"list", "get"},
    )
