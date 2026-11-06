"""Small theme request rules shared by Core and standalone execution."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


def backend_theme_data(name: str | None, mode: str | None) -> dict[str, Any]:
    if not name:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "theme_name is required when action='set'",
                context={"action": "set"},
                suggestions=[
                    "Call ha_manage_theme(action='list') to see installed themes"
                ],
            )
        )
    return {"name": name, **({"mode": mode} if mode is not None else {})}


def engine_theme_value(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "value is required when action='set_engine_theme'",
                context={"action": "set_engine_theme"},
                suggestions=[
                    "Use the value quoted in the screenshot tool's warning, e.g. {'theme': '', 'dark': False}"
                ],
            )
        )
    return value


def theme_writes(arguments: dict[str, Any]) -> bool:
    return arguments["action"] in {"set", "set_engine_theme"}
