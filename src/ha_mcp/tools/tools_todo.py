"""Todo management facade preserving the established MCP tool contracts."""

from typing import Any

from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.backends.legacy import LegacyBackendTools, legacy_errors
from ha_mcp.runtime.todo import TodoTools as SharedTodoTools

from .helpers import log_tool_usage, register_tool_methods
from .tool_hints import read_only_hints, write_hints


class TodoTools(SharedTodoTools, LegacyBackendTools):
    ha_get_todo = tool(
        name="ha_get_todo",
        tags={"Todo Lists"},
        annotations=read_only_hints("Get Todo", open_world=False),
    )(log_tool_usage(legacy_errors(SharedTodoTools.ha_get_todo)))

    ha_set_todo_item = tool(
        name="ha_set_todo_item",
        tags={"Todo Lists"},
        annotations=write_hints(
            "Set Todo Item", destructive=True, idempotent=False, open_world=False
        ),
    )(log_tool_usage(legacy_errors(SharedTodoTools.ha_set_todo_item)))

    ha_remove_todo_item = tool(
        name="ha_remove_todo_item",
        tags={"Todo Lists"},
        annotations=write_hints(
            "Remove Todo Item", destructive=True, idempotent=True, open_world=False
        ),
    )(log_tool_usage(legacy_errors(SharedTodoTools.ha_remove_todo_item)))


def register_todo_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the shared todo tools on the historical server."""
    register_tool_methods(mcp, TodoTools(client))
