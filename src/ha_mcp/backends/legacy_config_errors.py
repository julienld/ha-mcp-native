"""Preserve legacy ToolError payloads at shared authoring error seams."""

from typing import Any, NoReturn

from ha_mcp.tool_api import ToolFailure


def raise_legacy_classified(
    error: Exception, context: dict[str, Any], suggestions: list[str]
) -> NoReturn:
    from ha_mcp._vendor.fastmcp.exceptions import ToolError
    from ha_mcp.tools.helpers import exception_to_structured_error

    if isinstance(error, (ToolError, ToolFailure)):
        raise error
    exception_to_structured_error(error, context=context, suggestions=suggestions)
