"""Bind the shared service-group contracts to their Core entity owner."""

from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.group_tools import GroupTools
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints, write_hints

from .native_operations.groups import NativeGroups


def build_group_tools(manager: Any) -> list[ToolDefinition]:
    definitions = []
    for name, title, readonly, idempotent in (
        ("ha_config_list_groups", "List Groups", True, True),
        ("ha_config_set_group", "Create or Update Group", False, False),
        ("ha_config_remove_group", "Remove Group", False, True),
    ):

        async def invoke(
            arguments: dict[str, Any], actor: CallContext, method_name: str = name
        ) -> dict[str, Any]:
            groups = NativeGroups(
                manager.hass,
                actor,
                authorize=manager.backend.authorize,
                backup=manager.snapshots.group_backup,
            )
            method: Callable[..., Awaitable[dict[str, Any]]] = getattr(
                GroupTools(groups), method_name
            )
            return await method(**arguments)

        definitions.append(
            define_tool(
                manager.backend,
                GroupTools,
                name,
                source_module="tools_groups",
                tags=frozenset({"Groups"}),
                annotations=read_only_hints(title, open_world=False)
                if readonly
                else write_hints(
                    title, destructive=True, idempotent=idempotent, open_world=False
                ),
                handler=invoke,
            )
        )
    return definitions
