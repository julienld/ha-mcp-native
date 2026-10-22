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
