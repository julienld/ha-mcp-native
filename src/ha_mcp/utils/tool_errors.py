"""MCP error projection without importing a Home Assistant transport."""

import json
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.errors import TOOL_ERROR_LOG_LEVEL


def raise_tool_error(error_response: dict[str, Any]) -> NoReturn:
    """
    Raise a ToolError with structured error information.

    This function converts a structured error response dictionary into a ToolError
    exception, which signals to MCP clients that the tool execution failed via
    the isError flag in the protocol response.

    The structured error information is preserved as JSON in the error message,
    allowing AI agents to parse and act on the detailed error information.

    Args:
        error_response: Structured error response dictionary with 'success': False
                       and 'error' containing code, message, suggestions, etc.

    Raises:
        ToolError: Always raises with the JSON-serialized error response

    Example:
        >>> error = create_error_response(
        ...     ErrorCode.ENTITY_NOT_FOUND,
        ...     "Entity light.nonexistent not found"
        ... )
        >>> raise_tool_error(error)  # Raises ToolError with isError=true
    """
    raise ToolError(
        json.dumps(error_response, indent=2, default=str),
        log_level=TOOL_ERROR_LOG_LEVEL,
    )
