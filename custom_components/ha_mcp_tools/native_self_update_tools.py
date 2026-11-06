"""Bind the self-update facade once for the shared native MCP/Assist Engine."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.self_update_tools import SelfUpdateFacade
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_self_update import NativeSelfUpdate


def self_update_definition(manager: Any) -> ToolDefinition:
    service: NativeSelfUpdate | None = None

    async def invoke(arguments: dict[str, Any], actor: CallContext) -> Any:
        nonlocal service
        if service is None:
            service = NativeSelfUpdate(manager)
        bound = service

        async def manage(action: str) -> dict[str, Any]:
            return await bound.manage(action, actor)

        return await SelfUpdateFacade(manage).ha_manage_ha_mcp_updates(**arguments)

    return define_tool(
        manager.backend,
        SelfUpdateFacade,
        "ha_manage_ha_mcp_updates",
        handler=invoke,
        source_module="tools_self_update",
        tags=frozenset({"System"}),
        annotations=write_hints(
            "Update HA-MCP", destructive=True, idempotent=False, open_world=True
        ),
        writes=lambda arguments: (
            arguments.get("action", "check") not in ("check", "guide")
        ),
    )
