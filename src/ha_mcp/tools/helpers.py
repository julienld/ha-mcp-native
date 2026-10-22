"""
Reusable helper functions for MCP tools.

Centralized utilities that can be shared across multiple tool implementations.
"""

import functools
import logging
import time
from typing import Any, Literal, NoReturn, overload

from ha_mcp._vendor.fastmcp import Context
from ha_mcp.backends.legacy import legacy_errors_sync as _legacy_errors_sync
from ha_mcp.runtime.error_text import (
    extract_structured_error_reason as extract_structured_error_reason,
)
from ha_mcp.runtime.error_text import (
    extract_tool_error_message as extract_tool_error_message,
)
from ha_mcp.runtime.errors import ws_failure_code as ws_failure_code
from ha_mcp.runtime.validation import (
    reject_malformed_list_fields as _reject_malformed_list_fields,
)

from ..client.websocket_client import HomeAssistantWebSocketClient
from ..errors import (
    ErrorCode,
    create_connection_error,
)
from ..runtime.error_classification import (
    _ENVIRONMENT_CODES as _ENVIRONMENT_CODES,
)
from ..runtime.error_classification import (
    _append_macos_hints as _append_macos_hints,
)
from ..runtime.error_classification import (
    _classify_api_status as _classify_api_status,
)
from ..runtime.error_classification import (
    _classify_by_message as _classify_by_message,
)
from ..runtime.error_classification import (
    _classify_exception as _classify_exception,
)
from ..runtime.error_classification import (
    _with_classified_suggestions as _with_classified_suggestions,
)
from ..runtime.error_classification import classified_error
from ..runtime.validation import _QUOTE_ONLY as _QUOTE_ONLY
from ..runtime.validation import WHITESPACE_CLEARS_NOTE as WHITESPACE_CLEARS_NOTE
from ..runtime.validation import clear_hint as clear_hint
from ..tool_api.parameters import HIDDEN_PARAM as HIDDEN_PARAM
from ..tool_api.parameters import hidden_param_names as hidden_param_names
from ..utils.tool_errors import raise_tool_error as raise_tool_error
from ..utils.usage_logger import log_tool_call
from .coercion import UNSET

logger = logging.getLogger(__name__)


reject_malformed_list_fields = _legacy_errors_sync(_reject_malformed_list_fields)


def validate_identifier_not_empty(
    value: str | None,
    param_name: str,
    *,
    message: str | None = None,
    suggestions: list[str] | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """Keep the historical ToolError facade around the shared identifier policy."""
    from ha_mcp.runtime.validation import validate_identifier_not_empty as validate
    from ha_mcp.tool_api import ToolFailure

    try:
        return validate(
            value, param_name, message=message, suggestions=suggestions, context=context
        )
    except ToolFailure as error:
        raise_tool_error(error.data)


def clearable_value(
    value: str | None, param_name: str, *, hint: str | None = None
) -> str | None:
    """Map the shared clear-value policy back to the historical ToolError facade."""
    from ha_mcp.runtime.validation import clearable_value as clear
    from ha_mcp.tool_api import ToolFailure

    try:
        return clear(value, param_name, hint=hint)
    except ToolFailure as error:
        raise_tool_error(error.data)


async def get_connected_ws_client(
    base_url: str, token: str, verify_ssl: bool | None = None
) -> tuple[HomeAssistantWebSocketClient | None, dict[str, Any] | None]:
    """
    Create and connect a WebSocket client.

    Args:
        base_url: Home Assistant base URL
        token: Authentication token
        verify_ssl: TLS verification override. Pass ``client.verify_ssl``
            from the calling REST client so a programmatic
            ``HomeAssistantClient(verify_ssl=False)`` propagates to the
            WebSocket too. ``None`` falls back to ``settings.verify_ssl``.

    Returns:
        Tuple of (ws_client, error_dict). If connection fails, ws_client is None.
    """
    ws_client = HomeAssistantWebSocketClient(base_url, token, verify_ssl=verify_ssl)
    connected = await ws_client.connect()
    if not connected:
        reason = ws_client.last_connect_error
        details = (
            reason
            if isinstance(reason, str)
            else "WebSocket connection could not be established"
        )
        return None, create_connection_error(
            "Failed to connect to Home Assistant WebSocket",
            details=details,
        )
    return ws_client, None


# Environment failures: the classified guidance (token, admin rights, reachability)
# is what fixes them and most callers do not anticipate them, so it leads and a
# caller's own hints follow. Every other code keeps the caller's list as given.


@overload
def exception_to_structured_error(
    error: Exception,
    context: dict[str, Any] | None = None,
    *,
    raise_error: Literal[True] = True,
    suggestions: list[str] | None = None,
) -> NoReturn:
    pass


@overload
def exception_to_structured_error(
    error: Exception,
    context: dict[str, Any] | None = None,
    *,
    raise_error: Literal[False],
    suggestions: list[str] | None = None,
) -> dict[str, Any]:
    pass


def exception_to_structured_error(
    error: Exception,
    context: dict[str, Any] | None = None,
    *,
    raise_error: bool = True,
    suggestions: list[str] | None = None,
) -> dict[str, Any]:
    """
    Convert an exception to a structured error response.

    This function maps common exception types to appropriate error codes
    and creates informative error responses. By default, it raises a ToolError
    to signal the error at the MCP protocol level (isError=true).

    Args:
        error: The exception to convert
        context: Additional context to include in the response
        raise_error: If True (default), raises ToolError with the structured error.
                    If False, returns the error dict for further modification.
        suggestions: Optional list of actionable suggestions to embed in the error.
                    Saves callers from manually inserting suggestions after the call.
                    They replace the classified ones, except for the AUTH_*,
                    CONNECTION_FAILED and CONNECTION_TIMEOUT codes, where they
                    follow them (duplicates dropped).

    Returns:
        Structured error response dictionary (only if raise_error=False)

    Raises:
        ToolError: If raise_error=True (default), raises with JSON-serialized error
    """
    error_msg = str(error)
    error_response = classified_error(error, context, suggestions=suggestions)

    # Tracebacks are operationally valuable only for genuinely unclassified
    # exceptions (programmer errors, library bugs) — every other branch in
    # _classify_exception produces a structured signal that's sufficient on
    # its own. Logging the traceback here gives operators line numbers for the
    # bug class where ``str(error)`` is least informative, without
    # re-introducing the duplicate ERROR-log noise that classified failures
    # produced.
    #
    # ``exc_info=error`` rather than ``logger.exception()``: the traceback is
    # taken from the exception we were handed, not from ``sys.exc_info()``.
    # Every caller passes the exception explicitly, and the ``raise_error=False``
    # paths may run outside an ``except`` block, where ``sys.exc_info()`` is
    # empty and ``.exception()`` would log "NoneType: None" for the traceback.
    if (
        isinstance(error_response.get("error"), dict)
        and error_response["error"].get("code") == ErrorCode.INTERNAL_ERROR
    ):
        logger.error("Unclassified exception: %s", error_msg, exc_info=error)

    if raise_error:
        raise_tool_error(error_response)

    return error_response


def log_tool_usage(func: Any) -> Any:
    """
    Decorator to automatically log MCP tool usage.

    Tracks execution time, success/failure, and response size for all tool calls.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start_time = time.time()
        tool_name = func.__name__
        success = True
        error_message = None
        response_size = None

        try:
            result = await func(*args, **kwargs)
            if isinstance(result, str):
                response_size = len(result.encode("utf-8"))
            elif hasattr(result, "__len__"):
                response_size = len(str(result).encode("utf-8"))
            return result
        except BaseException as e:
            # BaseException, not Exception: a cancelled tool call must not be
            # recorded as a success. str() is empty on a bare CancelledError,
            # so the class name stands in.
            success = False
            error_message = str(e) or type(e).__name__
            raise
        finally:
            execution_time_ms = (time.time() - start_time) * 1000
            log_tool_call(
                tool_name=tool_name,
                # UNSET means the caller omitted the argument; ha_report_issue
                # returns these entries as JSON, so the sentinel must not land.
                parameters={k: v for k, v in kwargs.items() if v is not UNSET},
                execution_time_ms=execution_time_ms,
                success=success,
                error_message=error_message,
                response_size_bytes=response_size,
            )

    return wrapper


async def safe_progress(
    ctx: Context | None,
    *,
    progress: float,
    total: float | None = None,
    message: str | None = None,
) -> None:
    """Report progress via ``ctx.report_progress`` with best-effort error handling.

    A transport hiccup on a progress notification must never convert a
    successful tool result into a ``ToolError``. Transport errors are logged
    at debug; ``TypeError``/``AttributeError`` are escalated to ``warning``
    because they signal a signature/interface mismatch (call-site bug or
    Context object missing the expected method), not a flaky client.
    ``ctx is None`` short-circuits without I/O.
    """
    from ha_mcp.runtime.progress import report_progress

    await report_progress(
        (lambda **kwargs: ctx.report_progress(**kwargs)) if ctx is not None else None,  # noqa: PLW0108 - resolve attribute inside the common error boundary
        progress=progress,
        total=total,
        message=message,
    )


def clear_or_keep(value: str | None, param_name: str) -> str | None:
    """Preserve None-as-omitted and the historical ToolError facade."""
    from ha_mcp.runtime.validation import clear_or_keep as clear
    from ha_mcp.tool_api import ToolFailure

    try:
        return clear(value, param_name)
    except ToolFailure as error:
        raise_tool_error(error.data)


# Argument validation runs on the function signature, not the published schema,
# so a hidden parameter is still accepted from callers that pass it.


def register_tool_methods(mcp: Any, instance: Any) -> None:
    """Register all @tool-decorated methods from a class instance with the MCP server.

    Discovers methods bearing a ``__fastmcp__`` attribute (set by the outermost
    ``@tool`` decorator — must be listed above ``@log_tool_usage``) and registers
    them via ``mcp.add_tool()``, dropping ``HIDDEN_PARAM`` parameters from the
    published schema.
    """
    count = 0
    for attr in dir(instance):
        method = getattr(instance, attr)
        if callable(method) and hasattr(method, "__fastmcp__"):
            registered = mcp.add_tool(method)
            properties = getattr(registered, "parameters", {}).get("properties")
            if isinstance(properties, dict):
                for name in hidden_param_names(method):
                    properties.pop(name, None)
            count += 1
    if count == 0:
        logger.warning(f"No @tool-decorated methods found on {type(instance).__name__}")
