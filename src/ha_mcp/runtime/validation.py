"""Transport-neutral policies shared by existing and native tool facades."""

import re
from typing import Any, Literal

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


def validate_identifier_not_empty(
    value: str | None,
    param_name: str,
    *,
    message: str | None = None,
    suggestions: list[str] | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """Reject ``None``, empty, or whitespace-only identifier values.

    Surfaces a structured ``VALIDATION_INVALID_PARAMETER`` response so
    CRUD-style tools fail loudly on missing identifiers instead of silently
    routing on Python falsy semantics or relying on Home Assistant to
    translate a missing identifier into a generic ``RESOURCE_NOT_FOUND``.

    The destructive class this protects against:
    ``action = "update" if label_id else "create"`` — passing ``""`` as
    ``label_id`` silently routes to ``create`` when the caller intended
    ``update``. The whitespace class this protects against: ``" "`` is truthy
    in Python so ``if not value:`` lets it through, but Home Assistant has
    no entry with id ``" "``.

    The value is checked but not normalised: ``" abc "`` (a real id wrapped
    in spaces) is accepted as-is and returned untouched. Only purely empty or
    purely whitespace strings are rejected.

    ``None`` is also rejected by this helper. Callers for whom ``None`` is a
    documented sentinel (e.g. ``label_id=None`` meaning "list all" or
    "create new") must gate the call themselves with
    ``if value is not None: validate_identifier_not_empty(value, ...)``.

    Returning ``str`` (rather than ``None``) lets call sites use the helper
    in narrowing position — ``name = validate_identifier_not_empty(name, …)``
    re-binds ``name`` from ``str | None`` to ``str`` so mypy can prove later
    uses are safe without a duplicate inline check.

    Args:
        value: Identifier string supplied by the caller. ``None`` is
            rejected — see the ``None``-sentinel note above for the caller
            pattern that permits the sentinel.
        param_name: Name of the parameter, used in the structured error
            response's ``context`` and the human-readable message.
        message: Optional override for the human-readable error message.
            Defaults to ``"{param_name} must be a non-empty, non-whitespace
            string"`` when omitted.
        suggestions: Optional list of guidance strings for the response's
            ``suggestions`` field. Defaults to a generic
            "provide a non-empty value" hint.
        context: Optional additional context fields merged into the error
            response (e.g. ``{"action": "update"}``). The keys ``parameter``
            and ``value`` are always set and take precedence.

    Returns:
        The validated ``value`` unchanged (typed as ``str``).

    Raises:
        ToolFailure: When ``value`` is ``None``, empty, or whitespace-only —
            carrying a structured ``VALIDATION_INVALID_PARAMETER`` response
            with the parameter name, the offending value (for diagnostics),
            and the suggestions.

    Example:
        >>> validate_identifier_not_empty(label_id, "label_id",
        ...     suggestions=["Omit label_id to create a new label"])
    """
    if value is not None and value.strip():
        return value

    final_context: dict[str, Any] = {}
    if context:
        final_context.update(context)
    final_context["parameter"] = param_name
    final_context["value"] = value
    raise_failure(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            message or f"{param_name} must be a non-empty, non-whitespace string",
            suggestions=suggestions
            or [f"Provide a valid non-empty value for {param_name}"],
            context=final_context,
        )
    )


WHITESPACE_CLEARS_NOTE = (
    "A whitespace-only value acts like '', for clients that cannot send an "
    "empty string."
)


_QUOTE_ONLY = re.compile(r"[\s\"'‘’“”]+")


def clear_hint(param_name: str) -> str:
    return (
        f"To clear {param_name}, pass an empty string '', or a single space ' ' "
        "if your client cannot send an empty string"
    )


def clearable_value(
    value: str | None,
    param_name: str,
    *,
    hint: str | None = None,
) -> str | None:
    """Map an empty or whitespace-only value to None, the registry's clear value.

    Other values are returned stripped. A model whose client cannot send ``''``
    falls back to ``'""'`` (#2585); a value made only of quotes and whitespace
    is never a real value, so reject it.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if _QUOTE_ONLY.fullmatch(value):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{param_name} {value!r} contains only quote characters and whitespace",
                suggestions=[hint or clear_hint(param_name)],
                context={"parameter": param_name, "value": value},
            )
        )
    return value


def clear_or_keep(value: str | None, param_name: str) -> str | None:
    """``clearable_value`` for params where None means "not passed": blank → ''."""
    return None if value is None else clearable_value(value, param_name) or ""


def reject_malformed_list_fields(
    config: dict[str, Any],
    list_keys: tuple[str, ...],
    context: dict[str, Any],
    *,
    source: Literal["config", "patch", "python_transform"] = "config",
) -> None:
    """Reject list fields received as ``{"item": ...}`` or ``""`` (issue #2548).

    Home Assistant either rejects these shapes with an error that does not name
    them, or stores them and fails later, so the caller cannot tell what to fix.
    """
    received = {
        k: '{"item": ...}' if isinstance(v, dict) and list(v) == ["item"] else '""'
        for k in list_keys
        if (v := config.get(k)) == "" or (isinstance(v, dict) and list(v) == ["item"])
    }
    if not received:
        return
    names = ", ".join(f"'{k}'" for k in received)
    shapes = "; ".join(f"'{k}' as {shape}" for k, shape in received.items())
    first = next(iter(received))
    example = "[{...}, {...}]"
    if re.fullmatch(r"\w+", first):
        example = f'"{first}": {example}'
    if source == "python_transform":
        message = f"python_transform produced {shapes} instead of a list"
        suggestions = [f"Assign {names} a list, e.g. {example}, or delete an empty one"]
    else:
        noun = "config" if source == "config" else "patch value"
        message = f"Received {noun} {shapes} instead of a JSON array"
        suggestions = [
            f"Send {names} as a JSON array, e.g. {example}, or omit an empty one",
            f"If it still arrives malformed, pass the whole {source} as one "
            + "JSON-encoded string instead",
            "If neither works, check the model or MCP client in use",
        ]
    raise_failure(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"{message}; nothing was saved in Home Assistant",
            suggestions=suggestions,
            context={**context, "malformed_list_fields": received},
        )
    )
