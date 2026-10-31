"""Expose admitted dashboard renders as native MCP images."""

from dataclasses import replace
from typing import Any, cast

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.screenshot_tools import ScreenshotFacade
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolResult
from ha_mcp.tools.tool_hints import write_hints

from .native_screenshots import NativeScreenshots


def screenshot_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> ToolResult:
        return cast(
            ToolResult,
            await ScreenshotFacade(
                NativeScreenshots(manager, actor)
            ).ha_get_dashboard_screenshot(**arguments),
        )

    definition = define_tool(
        manager.backend,
        ScreenshotFacade,
        "ha_get_dashboard_screenshot",
        source_module="tools_dashboard_screenshot",
        tags=frozenset({"Dashboard", "beta"}),
        annotations=write_hints(
            "Get Dashboard Screenshot",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
        handler=invoke,
        meta={
            "ha_mcp": {
                "requires_image_content": True,
                "native_support": {
                    "note": "Every render requires write admission: the renderer may persist its user's profile, including without explicit theme parameters. A failed native render is not automatically retried."
                },
            }
        },
    )
    return replace(definition, output_schema=None)
