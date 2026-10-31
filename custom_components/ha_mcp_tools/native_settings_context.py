"""Carry the administrator panel session into owned native operations."""

from __future__ import annotations

import hashlib
from typing import Any

from ha_mcp.tool_api import CallContext

from .ui_panel import _COOKIE_NAME, _session_user_is_admin, _sessions


async def settings_context(hass: Any, request: Any) -> CallContext:
    """Bind the panel cookie to its current owner, with live revalidation."""
    token = request.cookies.get(_COOKIE_NAME)
    if not await _session_user_is_admin(hass, token):
        raise PermissionError("The administrator session is unavailable")
    session = _sessions(hass).get(token)
    if session is None:
        raise PermissionError("The administrator session is unavailable")
    user_id = str(session["user_id"])

    async def validate() -> None:
        if not await _session_user_is_admin(hass, token):
            raise PermissionError("The administrator session was revoked or expired")
        if _sessions(hass).get(token, {}).get("user_id") != user_id:
            raise PermissionError("The administrator session owner changed")

    return CallContext(
        user_id=user_id,
        client_id="settings:" + hashlib.sha256(token.encode()).hexdigest(),
        credential=validate,
    )
