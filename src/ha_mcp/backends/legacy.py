"""Compatibility decoration for existing FastMCP tool registrations."""

import functools
import inspect
from collections.abc import Callable
from typing import Any, cast, get_type_hints

from ha_mcp.tool_api import ToolFailure


def legacy_errors(function: Callable[..., Any]) -> Callable[..., Any]:
    """Keep the historical FastMCP error envelope on a shared implementation."""

    @functools.wraps(function)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        from ha_mcp._vendor.fastmcp.exceptions import ToolError
        from ha_mcp.tools.helpers import exception_to_structured_error, raise_tool_error

        try:
            return await function(*args, **kwargs)
        except ToolFailure as exc:
            raise_tool_error(exc.data)
        except ToolError:
            raise
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

    _preserve_resolved_signature(invoke, function)
    return invoke


def legacy_errors_sync(function: Callable[..., Any]) -> Callable[..., Any]:
    """Compatibility for existing synchronous validation/projection helpers."""

    @functools.wraps(function)
    def invoke(*args: Any, **kwargs: Any) -> Any:
        from ha_mcp.tools.helpers import raise_tool_error

        try:
            return function(*args, **kwargs)
        except ToolFailure as exc:
            raise_tool_error(exc.data)

    _preserve_resolved_signature(invoke, function)
    return invoke


def _preserve_resolved_signature(
    wrapper: Callable[..., Any], source: Callable[..., Any]
) -> None:
    """Future annotations must resolve in the source module, not this adapter."""
    hints = get_type_hints(source, include_extras=True)
    signature = inspect.signature(source)
    metadata = cast(Any, wrapper)
    metadata.__signature__ = signature.replace(
        parameters=[
            parameter.replace(annotation=hints.get(name, parameter.annotation))
            for name, parameter in signature.parameters.items()
        ],
        return_annotation=hints.get("return", signature.return_annotation),
    )
    metadata.__annotations__ = dict(hints)
    if hasattr(source, "__annotate__"):
        # Python 3.14 wraps copies __annotate__, not __annotations__. Preserve
        # concrete hints through outer decorators such as log_tool_usage.
        metadata.__annotate__ = lambda _format: dict(hints)
