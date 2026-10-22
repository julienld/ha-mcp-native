"""Helper semantic ports preserve the remote client's at-most-once boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext


async def test_remote_helper_storage_roundtrip_uses_existing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.tools import component_helper_collections

    monkeypatch.setattr(
        component_helper_collections, "_component_ws", AsyncMock(return_value=None)
    )
    stored = {}

    async def send(message: dict) -> dict:
        match message["type"]:
            case "input_boolean/create":
                stored.update(
                    {
                        "id": "example",
                        **{
                            key: value
                            for key, value in message.items()
                            if key != "type"
                        },
                    }
                )
                return {"success": True, "result": dict(stored)}
            case "input_boolean/list":
                return {"success": True, "result": [dict(stored)]}
            case "config/entity_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "input_boolean.renamed",
                            "platform": "input_boolean",
                            "unique_id": "example",
                        }
                    ],
                }
        raise AssertionError(message)

    client = SimpleNamespace(send_websocket_message=AsyncMock(side_effect=send))
    backend = RemoteBackend(client)
    context = CallContext("user")
    result = await backend.execute(
        "helpers.storage.write",
        {
            "helper_type": "input_boolean",
            "action": "create",
            "data": {"name": "Example", "initial": False},
        },
        context,
    )
    readback = await backend.execute(
        "helpers.storage.read",
        {"helper_type": "input_boolean", "item_id": "example"},
        context,
    )
    assert (
        result["item"]
        == readback["item"]
        == {"id": "example", "name": "Example", "initial": False}
    )
    assert result["entity_id"] == readback["entity_id"] == "input_boolean.renamed"
    assert (
        sum(
            call.args[0]["type"] == "input_boolean/create"
            for call in client.send_websocket_message.await_args_list
        )
        == 1
    )


async def test_helper_write_unknown_outcome_does_not_fallback_or_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.client.rest_client import HomeAssistantCommandTimeout
    from ha_mcp.tools import component_helper_collections

    socket = SimpleNamespace(
        send_command=AsyncMock(side_effect=HomeAssistantCommandTimeout("reply lost"))
    )
    monkeypatch.setattr(
        component_helper_collections, "_component_ws", AsyncMock(return_value=socket)
    )
    client = SimpleNamespace(send_websocket_message=AsyncMock())
    backend = RemoteBackend(client)
    context = CallContext("user")
    result = await Engine(backend).call_owned(
        "helper-write",
        lambda: backend.execute(
            "helpers.storage.write",
            {
                "helper_type": "input_boolean",
                "action": "create",
                "data": {"name": "Example"},
            },
            context,
        ),
        context,
        writes=True,
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["write_outcome_unknown"] is True
    socket.send_command.assert_awaited_once()
    client.send_websocket_message.assert_not_awaited()
