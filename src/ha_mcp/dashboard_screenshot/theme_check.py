"""Check a screenshot's requested theme against Home Assistant's own themes.

The engine renders an unknown theme name as the default theme and reports no
error, so the name is checked against Core's ``frontend/get_themes`` first.
"""

from __future__ import annotations

import logging
from typing import Any

from ..runtime.screenshot_theme_validation import (
    _BUILT_IN_THEME,
    _unchecked,
    validate_requested_theme,
)
from ..tool_api import ToolFailure

logger = logging.getLogger(__name__)

# The frontend's built-in theme; Core's theme list holds only installed ones.


async def check_requested_theme(
    client: Any, theme: str | None, dark_mode: bool
) -> list[str]:
    """Refuse a theme Home Assistant does not have.

    Returns warnings for a theme list that could not be read and for a
    ``dark_mode`` naming a mode the theme's data does not define.
    """
    if theme is None or client is None or theme == _BUILT_IN_THEME:
        return []

    from ..tools.helpers import raise_tool_error
    from ..tools.util_helpers import websocket_error_message

    try:
        result = await client.send_websocket_message({"type": "frontend/get_themes"})
    except Exception as exc:
        logger.warning(
            "frontend/get_themes failed; theme %r not checked: %s",
            theme,
            exc,
            exc_info=True,
        )
        return _unchecked(theme, str(exc))
    if isinstance(result, dict) and result.get("success") is False:
        return _unchecked(theme, websocket_error_message(result.get("error")))
    listing = result.get("result") if isinstance(result, dict) else None
    themes = listing.get("themes") if isinstance(listing, dict) else None
    try:
        return validate_requested_theme(themes, theme, dark_mode)
    except ToolFailure as error:
        return raise_tool_error(error.data)
