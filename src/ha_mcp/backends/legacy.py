"""Compatibility decoration for existing FastMCP tool registrations."""

import functools
import inspect
from collections.abc import Callable
from typing import Any

from ha_mcp.tool_api import ToolFailure


def legacy_errors(function: Callable[..., Any]) -> Callable[..., Any]:
    """Keep the historical FastMCP error envelope on a shared implementation."""

    @functools.wraps(function)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        from ha_mcp.tools.helpers import exception_to_structured_error, raise_tool_error

        try:
            return await function(*args, **kwargs)
        except ToolFailure as exc:
            raise_tool_error(exc.data)
        except Exception as exc:
            # The old classifier raises ToolError; do not log secrets from args.
            bound = inspect.signature(function).bind_partial(*args, **kwargs)
            entity_id = bound.arguments.get("entity_id")
            context = {"entity_id": entity_id} if isinstance(entity_id, str) else None
            suggestions = None
            if function.__name__ == "ha_get_state" and context:
                suggestions = [
                    f"Verify entity '{entity_id}' exists in Home Assistant",
                    "Check Home Assistant connection",
                    "Use ha_search() to find correct entity IDs",
                ]
            exception_to_structured_error(exc, context=context, suggestions=suggestions)
            raise

    return invoke
