"""Project the four public file contracts onto the component's loaded owner."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.filesystem_tools import FilesystemTools
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints, write_hints

from .native_operations.filesystem import NativeFilesystem


def filesystem_definitions(manager: Any) -> list[ToolDefinition]:
    def definition(name: str, annotations: dict[str, Any]) -> ToolDefinition:
        async def invoke(
            arguments: dict[str, Any], actor: CallContext
        ) -> dict[str, Any]:
            tools = FilesystemTools(NativeFilesystem(manager, actor))
            result: dict[str, Any] = await getattr(tools, name)(**arguments)
            return result

        return define_tool(
            manager.backend,
            FilesystemTools,
            name,
            handler=invoke,
            source_module="tools_filesystem",
            tags=frozenset({"Files", "beta"}),
            annotations=annotations,
        )

    return [
        definition("ha_list_files", read_only_hints("List Files", open_world=False)),
        definition("ha_read_file", read_only_hints("Read File", open_world=False)),
        definition(
            "ha_write_file",
            write_hints(
                "Write File", destructive=True, idempotent=True, open_world=False
            ),
        ),
        definition(
            "ha_delete_file",
            write_hints(
                "Delete File", destructive=True, idempotent=True, open_world=False
            ),
        ),
    ]
