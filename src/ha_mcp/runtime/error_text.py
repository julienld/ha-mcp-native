"""Pure text projection of serialized tool errors."""

import json


def extract_tool_error_message(te: BaseException | str) -> str:
    """Extract a human-readable error message from a ToolError or its logged text.

    Pairs with raise_tool_error() which serializes error dicts as JSON.
    Falls back to str(te) if the message is not valid JSON.
    """
    try:
        error_data = json.loads(str(te))
        msg = error_data.get("error", {}).get("message", str(te))
        return str(msg)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return str(te)


def extract_structured_error_reason(exc: BaseException) -> str | None:
    """Extract "message" + first actionable suggestion from a structured error.

    Like ``extract_tool_error_message`` but for surfaces that show the reason
    to a human (settings UI, degraded-result warnings): it appends the first
    suggestion when one exists, and returns ``None`` — instead of the raw
    ``str(exc)`` JSON envelope — when the payload is not a structured error
    or carries no usable message, so callers pick their own fallback.
    ``create_error_response`` emits only the singular ``suggestion`` key for
    one-suggestion errors (the plural ``suggestions`` list needs two or
    more), so both keys are honored.
    """
    try:
        payload = json.loads(str(exc))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    if not isinstance(message, str) or not message:
        return None
    suggestions = error.get("suggestions")
    first = (
        suggestions[0]
        if isinstance(suggestions, list) and suggestions
        else error.get("suggestion")
    )
    if isinstance(first, str) and first:
        return f"{message} {first}"
    return message
