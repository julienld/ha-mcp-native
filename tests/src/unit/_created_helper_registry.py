"""A WebSocket fake for helper creation without the component.

Only the entity registry proves which entity a new storage helper got, so the
create path reads ``config/entity_registry/list`` after Core's create.
"""

from typing import Any
from unittest.mock import AsyncMock

CREATED_ENTRY = {
    "entity_id": "input_boolean.abc123",
    "platform": "input_boolean",
    "unique_id": "abc123",
}


def with_registry(answer: Any) -> AsyncMock:
    """Answer the entity registry list with the created helper's entry and every
    other command with ``answer`` (a reply, or an async handler)."""

    async def send(msg: dict) -> Any:
        if msg.get("type") == "config/entity_registry/list":
            return {"success": True, "result": [CREATED_ENTRY]}
        return await answer(msg) if callable(answer) else answer

    return AsyncMock(side_effect=send)


def with_resolved_registry(answer: Any) -> AsyncMock:
    """Make a stable fake registry list contain its successful get responses."""
    entries: dict[str, dict[str, Any]] = {}

    async def send(message: dict[str, Any]) -> Any:
        if message["type"] == "config/entity_registry/list":
            return {"success": True, "result": list(entries.values())}
        response = await answer(message)
        if message["type"] == "config/entity_registry/get" and response.get("success"):
            row = response["result"]
            entries[row["entity_id"]] = dict(row)
        return response

    return AsyncMock(side_effect=send)
