"""Use the established public error envelopes without importing a transport."""

from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode
from ha_mcp.tool_api import ToolFailure


def raise_failure(payload: dict[str, Any]) -> NoReturn:
    error = payload.get("error", {})
    raise ToolFailure(
        error.get("code", "TOOL_ERROR") if isinstance(error, dict) else "TOOL_ERROR",
        error.get("message", "Tool failed") if isinstance(error, dict) else str(error),
        data=payload,
    )


def raise_classified(
    error: Exception,
    context: dict[str, Any] | None = None,
    *,
    suggestions: list[str] | None = None,
) -> NoReturn:
    """Keep the historical classifier without depending on MCP or a client."""
    from .error_classification import classified_error

    if isinstance(error, ToolFailure):
        raise error
    raise_failure(classified_error(error, context, suggestions=suggestions))


def ws_failure_code(result: dict[str, Any]) -> ErrorCode:
    """Map Core's WS error code: ``invalid_format`` is the caller's input,
    ``not_found`` an item Core does not hold (e.g. a YAML-configured helper)."""
    code = result.get("error_code")
    if code == "invalid_format":
        return ErrorCode.VALIDATION_INVALID_PARAMETER
    if code == "not_found":
        return ErrorCode.RESOURCE_NOT_FOUND
    return ErrorCode.SERVICE_CALL_FAILED


def websocket_error_message(error: Any) -> str:
    """Extract a readable message from a Home Assistant websocket error."""
    if isinstance(error, dict):
        return str(error.get("message", error))
    return str(error)
