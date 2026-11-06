"""Theme validation over a real Core theme snapshot, independent of transport."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure

_BUILT_IN_THEME = "default"


def _unchecked(theme: str, reason: str) -> list[str]:
    return [
        f"Home Assistant's theme list could not be read ({reason}), so theme "
        f"{theme!r} was not checked; an unknown name renders the default theme."
    ]


def validate_requested_theme(
    themes: Any, theme: str | None, dark_mode: bool
) -> list[str]:
    if theme is None or theme == _BUILT_IN_THEME:
        return []
    if not isinstance(themes, dict):
        return _unchecked(theme, "unexpected response")
    if theme not in themes:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Home Assistant has no theme named {theme!r}",
                context={"installed_themes": [_BUILT_IN_THEME, *themes]},
                suggestions=["Use one of installed_themes, or omit theme"],
            ),
            status="not_started",
        )
    entry = themes[theme]
    modes = entry.get("modes") if isinstance(entry, dict) else None
    if not isinstance(modes, dict):
        modes = {}
    if dark_mode and "dark" not in modes:
        return [
            f"Theme {theme!r} defines no dark mode in Home Assistant's theme list, so dark_mode=true has no dark variant to use."
        ]
    if not dark_mode and modes and ("light" not in modes):
        return [
            f"Theme {theme!r} defines only a dark mode in Home Assistant's theme list, so dark_mode=false has no light variant to use."
        ]
    return []
