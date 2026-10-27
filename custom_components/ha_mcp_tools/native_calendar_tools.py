"""Bind calendar metadata to the actual CalendarEntity service per invocation."""

from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp.runtime.calendar_tools import CalendarTools
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints, write_hints

from .native_operations.calendar_events import NativeCalendars


def build_calendar_tools(manager: Any) -> list[ToolDefinition]:
    definitions = []
    for name, title, readonly, idempotent in (
        ("ha_config_get_calendar_events", "Get Calendar Events", True, True),
        (
            "ha_config_set_calendar_event",
            "Create or Update Calendar Event",
            False,
            False,
        ),
        ("ha_config_remove_calendar_event", "Remove Calendar Event", False, True),
    ):

        async def invoke(
            arguments: dict[str, Any], actor: CallContext, method_name: str = name
        ) -> dict[str, Any]:
            service = NativeCalendars(
                manager.hass,
                actor,
                authorize=manager.backend.authorize,
                backup=manager.snapshots.calendar_backup,
                lookahead_days=int(
                    manager.snapshots._value("auto_backup_calendar_lookahead_days")
                ),
            )
            method: Callable[..., Awaitable[dict[str, Any]]] = getattr(
                CalendarTools(service), method_name
            )
            return await method(**arguments)

        definitions.append(
            define_tool(
                manager.backend,
                CalendarTools,
                name,
                source_module="tools_calendar",
                annotations=read_only_hints(title, open_world=False)
                if readonly
                else write_hints(
                    title, destructive=True, idempotent=idempotent, open_world=False
                ),
                tags=frozenset({"Calendar"}),
                handler=invoke,
            )
        )
    return definitions
