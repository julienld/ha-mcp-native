"""Validate and normalize the shared MCP call-proxy argument envelope."""

import json
import logging
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.errors import TOOL_ERROR_LOG_LEVEL, ErrorCode, create_error_response

logger = logging.getLogger(__name__)


def _raise_non_object_arguments(value: Any, proxy_name: str, name: str) -> NoReturn:
    """Refuse a proxy ``arguments`` payload that is not an object."""
    raise ToolError(
        json.dumps(
            create_error_response(
                code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                message=(
                    f"'arguments' must be a JSON object (got {type(value).__name__})."
                ),
                suggestions=[
                    "Pass 'arguments' as an object (dict), not a list or scalar.",
                ],
                context={"proxy_used": proxy_name, "tool_name": name},
            )
        ),
        log_level=TOOL_ERROR_LOG_LEVEL,
    )


def _coerce_proxy_arguments(
    arguments: dict[str, Any] | str | None,
    proxy_name: str,
    name: str,
) -> dict[str, Any] | None:
    """Coerce a proxy call's ``arguments`` to a dict, tolerating a JSON string.

    Small models sometimes serialize ``arguments`` before sending. Parse once up
    front so downstream logic can assume a dict (or None).
    """
    if not isinstance(arguments, str):
        return arguments
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, RecursionError) as e:
        # RecursionError: a string nested past the interpreter's limit is
        # still "not valid JSON" to the caller, not an internal error.
        raise ToolError(
            json.dumps(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_JSON,
                    message=f"'arguments' is a string but not valid JSON: {e}",
                    suggestions=[
                        "Pass 'arguments' as an object, not a JSON string.",
                    ],
                    context={"proxy_used": proxy_name, "tool_name": name},
                )
            ),
            log_level=TOOL_ERROR_LOG_LEVEL,
        ) from e
    if not isinstance(parsed, dict):
        _raise_non_object_arguments(parsed, proxy_name, name)
    logger.warning(
        "Proxy %s received 'arguments' as a JSON string for tool %s — parsed as fallback",
        proxy_name,
        name,
    )
    return parsed
