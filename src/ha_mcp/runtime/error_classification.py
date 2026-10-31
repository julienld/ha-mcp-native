"""Transport-neutral exception classification, shared with the legacy facade."""

import re
import sys
from typing import Any

from ha_mcp.errors import (
    NON_ADMIN_TOKEN_WARNING,
    ErrorCode,
    HomeAssistantAdminRequiredError,
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
    create_auth_error,
    create_connection_error,
    create_entity_not_found_error,
    create_error_response,
    create_timeout_error,
    create_validation_error,
)

from .service_outcome import (
    create_service_outcome_unknown_error,
    is_service_outcome_unknown,
)


def _classify_api_status(
    error: HomeAssistantAPIError,
    error_msg: str,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify HomeAssistantAPIError by HTTP status code."""
    match error.status_code:
        case 404:
            entity_id = context.get("entity_id") if context else None
            if entity_id:
                result = create_entity_not_found_error(entity_id, details=error_msg)
            else:
                result = create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND, error_msg, context=context
                )
        case 401 | 403:
            result = create_auth_error(error_msg, context=context)
        case 400:
            result = create_validation_error(error_msg, context=context)
        case _:
            result = create_error_response(
                ErrorCode.SERVICE_CALL_FAILED, error_msg, context=context
            )
    return result


def _classify_exception(
    error: Exception,
    error_str: str,
    error_msg: str,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify exception into structured error response by type, then message."""
    result: dict[str, Any] | None = None

    # Type-based classification
    match error:
        case HomeAssistantConnectionError():
            result = create_connection_error(
                error_msg, timeout="timeout" in error_str, context=context
            )
        case HomeAssistantAdminRequiredError():
            result = create_error_response(
                ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS,
                error_msg,
                suggestions=[
                    "Use a long-lived access token from an administrator's profile",
                    "Or use the in-process server, which provisions its own admin token",
                ],
                context=context,
            )
            result["warnings"] = [NON_ADMIN_TOKEN_WARNING]
        case HomeAssistantAuthError():
            result = create_auth_error(
                error_msg, expired="expired" in error_str, context=context
            )
        case HomeAssistantAPIError():
            result = _classify_api_status(error, error_msg, context)
        case HomeAssistantCommandError():
            # WebSocket command-failure. The ``error.code`` on Supervisor
            # calls routed through HA Core's hassio WS bridge is always
            # ``unknown_error`` (see homeassistant/components/hassio/
            # websocket_api.py), so discrimination must come from the
            # message. Fall through to ``_classify_by_message`` which
            # pattern-matches schema, auth, not-found and timeout cases.
            result = None
        case TimeoutError():
            operation = context.get("operation", "request") if context else "request"
            timeout_seconds = context.get("timeout_seconds", 30) if context else 30
            result = create_timeout_error(
                operation, timeout_seconds, details=error_msg, context=context
            )
        case ValueError():
            result = create_validation_error(error_msg, context=context)

    if result is not None:
        return result

    # Message-based classification fallback
    return _classify_by_message(error_str, error_msg, context)


def _classify_by_message(
    error_str: str,
    error_msg: str,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify exception by error message patterns."""
    result: dict[str, Any]
    # Schema-branch must precede the "not found" / 404 branch (most-specific-first):
    # a vol.Invalid message phrased like "Command failed: key X not found" would
    # otherwise misclassify as RESOURCE_NOT_FOUND. The "command failed:" prefix
    # gates the branch so non-schema WS errors fall through.
    if "command failed:" in error_str and (
        any(
            marker in error_str
            for marker in (
                "missing option",
                "extra keys not allowed",
                "unknown secret",
                "unknown type",
            )
        )
        or re.search(
            r"expected (?:a |str|int|bool|dict|list|float|type|one of)", error_str
        )
    ):
        # Supervisor schema validation: vol.Invalid message arriving as a
        # HomeAssistantCommandError via HA Core's hassio WS bridge. The
        # markers plus the "expected <type>" anchor regex cover the
        # heterogeneous vol.Invalid vocabulary without relying on an
        # error code (always unknown_error from the bridge).
        result = create_validation_error(error_msg, context=context)
    elif (
        "not found" in error_str
        or "404" in error_str
        or "unknown config specified" in error_str
    ):
        # ``unknown config specified`` is HA Core's WS-bridge phrasing for
        # missing-dashboard 404s (lovelace/config with an unknown url_path).
        # The string contains neither ``not found`` nor ``404``, so it would
        # otherwise fall through to the ``command failed:`` SERVICE_CALL_FAILED
        # fallback below and the agent would lose the not-found signal.
        entity_id = context.get("entity_id") if context else None
        if entity_id:
            result = create_entity_not_found_error(entity_id, details=error_msg)
        else:
            result = create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND, error_msg, context=context
            )
    elif "timeout" in error_str:
        # Honor caller-supplied context like the type-based TimeoutError
        # branch does — a hardcoded 30 here misreported calls that run on a
        # longer budget (e.g. the 60 s HACS refresh/remove round-trips).
        # WS callers name the call ``command`` rather than ``operation``, so
        # fall back to it: without that the message reads "Operation
        # 'operation' timed out" and never names what actually timed out.
        operation = (
            (context.get("operation") or context.get("command") or "operation")
            if context
            else "operation"
        )
        timeout_seconds = context.get("timeout_seconds", 30) if context else 30
        result = create_timeout_error(
            operation, timeout_seconds, details=error_msg, context=context
        )
    elif "connection" in error_str or "connect" in error_str:
        result = create_connection_error(error_msg, context=context)
    elif (
        any(
            phrase in error_str
            for phrase in (
                "unauthorized",
                "authentication",
                "invalid token",
                "access denied",
            )
        )
        or "401" in error_str
    ):
        result = create_auth_error(error_msg, context=context)
    elif error_str.startswith("command failed:"):
        # HomeAssistantCommandError fallback: WS ``success=False`` with a
        # message that doesn't match any specific marker above. This is a
        # known failure mode (the WS command itself failed), not an
        # unexpected internal error — route to SERVICE_CALL_FAILED,
        # mirroring the 4xx fallback in _classify_api_status.
        result = create_error_response(
            ErrorCode.SERVICE_CALL_FAILED, error_msg, context=context
        )
    else:
        result = create_error_response(
            ErrorCode.INTERNAL_ERROR,
            "An unexpected error occurred",
            details=error_msg,
            context=context,
        )
    return result


_ENVIRONMENT_CODES = frozenset(
    {
        ErrorCode.AUTH_INVALID_TOKEN.value,
        ErrorCode.AUTH_EXPIRED.value,
        ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS.value,
        ErrorCode.CONNECTION_FAILED.value,
        ErrorCode.CONNECTION_TIMEOUT.value,
    }
)


def _with_classified_suggestions(
    error: dict[str, Any], suggestions: list[str]
) -> list[str]:
    """Put the classified suggestions of an environment code before the caller's."""
    if error.get("code") not in _ENVIRONMENT_CODES:
        return suggestions
    classified = error.get("suggestions") or (
        [error["suggestion"]] if error.get("suggestion") else []
    )
    return [*classified, *(s for s in suggestions if s not in classified)]


def _append_macos_hints(error_response: dict[str, Any]) -> None:
    if not (
        sys.platform == "darwin"
        and "error" in error_response
        and isinstance(error_response["error"], dict)
        and error_response["error"].get("code")
        in (ErrorCode.CONNECTION_FAILED, ErrorCode.CONNECTION_TIMEOUT)
    ):
        return
    macos_hints = [
        "macOS may block local network access for Claude Desktop subprocesses "
        + "(System Settings > Privacy & Security > Local Network)",
        "Try an SSH tunnel: ssh -N -L 8123:localhost:8123 user@ha-server, "
        + "then use http://localhost:8123",
        "Ensure you are using http:// (not https://) unless SSL/TLS is configured",
    ]
    # Handle both "suggestions" (plural, 2+ items) and "suggestion" (singular, 1 item)
    existing = error_response["error"].get("suggestions") or []
    if not existing:
        single = error_response["error"].get("suggestion")
        if single:
            existing = [single]
    error_response["error"]["suggestions"] = existing + macos_hints


def classified_error(
    error: Exception,
    context: dict[str, Any] | None = None,
    *,
    suggestions: list[str] | None = None,
) -> dict[str, Any]:
    if is_service_outcome_unknown(error):
        response = create_service_outcome_unknown_error(error, context=context)
        if suggestions:
            diagnostic = response["error"]
            existing = diagnostic.get("suggestions") or [diagnostic["suggestion"]]
            guidance = [
                *existing,
                *(item for item in suggestions if item not in existing),
            ]
            diagnostic["suggestion"] = guidance[0]
            diagnostic["suggestions"] = guidance
        return response
    error_response = _classify_exception(error, str(error).lower(), str(error), context)
    if (
        suggestions
        and "error" in error_response
        and isinstance(error_response["error"], dict)
    ):
        # Set both `suggestion` (singular, first item) and `suggestions`
        # (plural, full list). create_error_response (errors.py) sets the
        # singular key; existing tests for exception_to_structured_error
        # rely on the plural key being present even for single-item caller
        # suggestions. Setting both keeps response consumers on both code
        # paths working.
        suggestions = _with_classified_suggestions(error_response["error"], suggestions)
        error_response["error"]["suggestion"] = suggestions[0]
        error_response["error"]["suggestions"] = suggestions

    # Append macOS-specific hints for connection failures (after all other processing
    # so hints survive regardless of whether caller provided explicit suggestions)
    _append_macos_hints(error_response)

    return error_response
