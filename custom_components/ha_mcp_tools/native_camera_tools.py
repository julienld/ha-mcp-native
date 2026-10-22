"""Project the shared camera contract into bounded native image content."""

from dataclasses import replace
from typing import Any

from ha_mcp.runtime.camera_tools import CameraTools
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolImage, ToolResult
from ha_mcp.tools.tool_hints import read_only_hints

from .native_operations.cameras import NativeCameras


def camera_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> ToolResult:
        cameras = NativeCameras(
            manager.hass, actor, authorize=manager.backend.authorize
        )
        snapshot = await CameraTools(cameras).ha_get_camera_image(**arguments)
        return ToolResult(
            snapshot.info,
            images=(ToolImage(snapshot.data, "image/" + snapshot.image_format),),
        )

    definition = define_tool(
        manager.backend,
        CameraTools,
        "ha_get_camera_image",
        source_module="tools_camera",
        tags=frozenset({"Camera"}),
        annotations=read_only_hints("Get Camera Image", open_world=False),
        handler=invoke,
        meta={"ha_mcp": {"requires_image_content": True}},
    )
    return replace(definition, output_schema=None)
