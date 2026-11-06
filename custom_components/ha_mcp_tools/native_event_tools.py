"""Bind the event contract to Core; no HTTP client or WebSocket connection."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.event_tools import EventTools
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.events import NativeEvents


def event_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        events = NativeEvents(manager.hass, actor, authorize=manager.backend.authorize)
        return await EventTools(events).ha_call_event(**arguments)

    return define_tool(
        manager.backend,
        EventTools,
        "ha_call_event",
        source_module="tools_service",
        tags=frozenset({"Service & Device Control"}),
        annotations=write_hints(
            "Call Event", destructive=True, idempotent=False, open_world=False
        ),
        handler=invoke,
    )
