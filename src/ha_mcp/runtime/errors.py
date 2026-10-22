"""Use the established public error envelopes without importing a transport."""

from typing import Any, NoReturn

from ha_mcp.tool_api import ToolFailure


def raise_failure(payload: dict[str, Any]) -> NoReturn:
    error = payload.get("error", {})
    raise ToolFailure(
        error.get("code", "TOOL_ERROR"),
        error.get("message", "Tool failed"),
        data=payload,
    )


def raise_classified(
    error: Exception, context: dict[str, Any] | None = None
) -> NoReturn:
    """Keep the historical classifier without depending on MCP or a client."""
    from .error_classification import _append_macos_hints, _classify_exception

    if isinstance(error, ToolFailure):
        raise error
    payload = _classify_exception(error, str(error).lower(), str(error), context)
    _append_macos_hints(payload)
    raise_failure(payload)
