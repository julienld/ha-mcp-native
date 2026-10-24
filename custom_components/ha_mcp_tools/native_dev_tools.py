"""Bind the single developer contract to this manager's native owners."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.dev_tools import DevFacade
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.developer_server import NativeDeveloperServer


def dev_definitions(manager: Any) -> list[ToolDefinition]:
    definitions = []
    for name, title, open_world in (
        ("ha_dev_manage_settings", "Manage Server Settings (dev)", False),
        ("ha_dev_manage_server", "Manage MCP Server (dev)", True),
    ):

        async def invoke(
            arguments: dict[str, Any], actor: CallContext, method: str = name
        ) -> dict[str, Any]:
            result: dict[str, Any] = await getattr(
                DevFacade(NativeDeveloperServer(manager, actor)), method
            )(**arguments)
            return result

        definition = define_tool(
            manager.backend,
            DevFacade,
            name,
            handler=invoke,
            source_module="tools_dev",
            tags=frozenset({"Developer"}),
            annotations=write_hints(
                title, destructive=True, idempotent=False, open_world=open_world
            ),
            meta={
                "ha_mcp": {
                    "native_support": {
                        "note": "Native HACS refuses update_source before dispatch: update the integration through HACS. Only native settings are listed. Saved configuration is revalidated after writes; a partial persistence failure identifies confirmed files and requires a fresh read."
                    }
                }
            },
        )
        definitions.append(definition)
    return definitions
