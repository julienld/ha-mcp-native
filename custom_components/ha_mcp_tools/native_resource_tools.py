"""Bind the single public resource contract to Core-owned service instances."""

from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.resource_tools import ResourceTools
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.lovelace_resources import (
    NativeResources,
    resources_need_loading,
)


def build_resource_tools(manager: Any) -> list[ToolDefinition]:
    definitions = []
    for name, title, idempotent in (
        ("ha_config_list_dashboard_resources", "List Dashboard Resources", False),
        ("ha_config_set_dashboard_resource", "Set Dashboard Resource", False),
        ("ha_config_delete_dashboard_resource", "Delete Dashboard Resource", True),
    ):

        async def invoke(
            arguments: dict[str, Any], actor: CallContext, method_name: str = name
        ) -> dict[str, Any]:
            tools = ResourceTools(
                NativeResources(
                    manager.hass,
                    actor,
                    authorize=manager.backend.authorize,
                    backup=manager.snapshots.resource_backup,
                )
            )
            method: Callable[..., Awaitable[dict[str, Any]]] = getattr(
                tools, method_name
            )
            return await method(**arguments)

        listing = name == "ha_config_list_dashboard_resources"
        definitions.append(
            define_tool(
                manager.backend,
                ResourceTools,
                name,
                source_module="tools_resources",
                annotations=write_hints(
                    title, destructive=True, idempotent=idempotent, open_world=listing
                ),
                tags=frozenset({"Dashboards"}),
                handler=invoke,
                writes=(lambda _arguments: resources_need_loading(manager.hass))
                if listing
                else None,
                meta={
                    "ha_mcp": {
                        "native_support": {
                            "note": "The first resource read can initialize Core storage and migrate legacy resources out of the default dashboard. It requires write admission until Core has loaded the collection."
                        }
                    }
                }
                if listing
                else {},
            )
        )
    return definitions
