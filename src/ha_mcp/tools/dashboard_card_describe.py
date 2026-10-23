"""Describe Lovelace card types from Home Assistant's own frontend (issue #2632).

The ``ha_mcp_tools`` component reads each card editor's form from the frontend
Home Assistant serves; this module compacts it the way helper ``describe`` does.
"""

from __future__ import annotations

from typing import Any, cast

from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime.dashboard_card_values import describe_card_result

from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import exception_to_structured_error, raise_tool_error

WS_DASHBOARD_CARDS = "ha_mcp_tools/dashboard_cards"


async def describe_card_response(client: Any, card_type: str | None) -> dict[str, Any]:
    """The card type list, or one card type's fields as its UI editor defines them."""
    card_type = card_type or None
    caps = await get_component_caps(client)
    if not component_supports(caps, "dashboard_cards"):
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "Describing card types needs the ha_mcp_tools custom component "
                "with dashboard card support.",
                suggestions=[
                    "Install or update the HA-MCP Custom Component through HACS",
                    "Restart Home Assistant after the update completes",
                ],
                context={"action": "describe", "card_type": card_type},
            )
        )
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        kwargs = {"card_type": card_type} if card_type else {}
        raw = await ws.send_command(WS_DASHBOARD_CARDS, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if is_unknown_command(exc):
            invalidate_caps(client)
        exception_to_structured_error(
            exc, context={"action": "describe", "card_type": card_type}
        )
    result = raw.get("result") if isinstance(raw, dict) else None
    if not isinstance(result, dict):
        result = {}
    return cast(
        dict[str, Any], legacy_errors_sync(describe_card_result)(result, card_type)
    )


__all__ = [
    "describe_card_response",
]
