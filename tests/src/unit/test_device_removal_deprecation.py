"""Device removal uses the current command and only retries a proven absence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools.tools_registry import _remove_device_config_entries


async def test_device_removal_uses_current_command_once() -> None:
    client = SimpleNamespace(
        send_websocket_message=AsyncMock(return_value={"success": True})
    )
    result = await _remove_device_config_entries(client, "device", ["entry"])
    assert result == [{"config_entry_id": "entry", "success": True, "error": None}]
    client.send_websocket_message.assert_awaited_once_with(
        {"type": "config/device_registry/remove", "device_id": "device"}
    )


async def test_august_device_removal_falls_back_only_after_unknown_command() -> None:
    client = SimpleNamespace(
        send_websocket_message=AsyncMock(
            side_effect=[
                {"success": False, "error_code": "unknown_command"},
                {"success": True},
            ]
        )
    )
    result = await _remove_device_config_entries(client, "device", ["entry"])
    assert result[0]["success"]
    assert [
        call.args[0]["type"] for call in client.send_websocket_message.await_args_list
    ] == [
        "config/device_registry/remove",
        "config/device_registry/remove_config_entry",
    ]


@pytest.mark.parametrize("code", ["unauthorized", "unknown_error", "outcome_unknown"])
async def test_device_removal_does_not_retry_a_refusal_or_uncertain_result(
    code: str,
) -> None:
    client = SimpleNamespace(
        send_websocket_message=AsyncMock(
            return_value={"success": False, "error_code": code, "error": "refused"}
        )
    )
    result = await _remove_device_config_entries(client, "device", ["entry"])
    assert result == [
        {"config_entry_id": "entry", "success": False, "error": "refused"}
    ]
    assert client.send_websocket_message.await_count == 1
    assert (
        client.send_websocket_message.await_args.args[0]["type"]
        == "config/device_registry/remove"
    )


async def test_device_removal_does_not_retry_after_a_transport_timeout() -> None:
    client = SimpleNamespace(send_websocket_message=AsyncMock(side_effect=TimeoutError))
    with pytest.raises(TimeoutError):
        await _remove_device_config_entries(client, "device", ["entry"])
    assert client.send_websocket_message.await_count == 1
