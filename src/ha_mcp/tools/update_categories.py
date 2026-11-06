"""The category ``ha_manage_updates`` files an update entity under."""

import logging
from typing import Any

from ha_mcp.runtime.update_values import categorize_update as categorize_update

logger = logging.getLogger(__name__)

__all__ = ["categorize_update", "update_platforms"]


async def update_platforms(client: Any) -> dict[str, str]:
    """Map each registered update entity to the integration that owns it."""
    reply = await client.send_websocket_message(
        {"type": "config/entity_registry/list_for_display"}
    )
    if not reply.get("success"):
        logger.debug("Entity registry unavailable for update categories: %s", reply)
        return {}
    return {
        row["ei"]: row.get("pl", "")
        for row in (reply.get("result") or {}).get("entities", [])
        if row.get("ei", "").startswith("update.")
    }
