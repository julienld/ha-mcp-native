"""Legacy read failures keep their context and guidance through the real SDK."""

import json
import socket
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp import Client, Context, FastMCP
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    SceneStorageConfigNotFoundError,
)
from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import ToolFailure
from ha_mcp.tools import helpers, tools_services
from ha_mcp.tools.backup import BackupTools, register_backup_tools
from ha_mcp.tools.component_api import ComponentCaps
from ha_mcp.tools.search import state as search_state
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools
from ha_mcp.tools.tools_search import register_search_tools


@pytest.fixture(autouse=True)
async def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install after event-loop setup so Windows' own socket pair is unaffected."""

    def refused(*args: Any, **kwargs: Any) -> None:
        pytest.fail("This SDK regression test must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)
    monkeypatch.setattr(socket, "create_connection", refused)


@pytest.fixture
def read_server(monkeypatch: pytest.MonkeyPatch) -> tuple[FastMCP, MagicMock]:
    client = MagicMock(
        base_url="http://ha.invalid:8123",
        token="test-only",
        get_entity_state=AsyncMock(),
        get_services=AsyncMock(),
        get_config=AsyncMock(return_value={"time_zone": "UTC"}),
    )
    monkeypatch.setattr(
        search_state, "get_component_caps", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        tools_services, "get_component_caps", AsyncMock(return_value=None)
    )
    server = FastMCP("legacy-read-errors")
    register_search_tools(server, client, smart_tools=object())
    tools_services.register_services_tools(server, client)
    return server, client


async def test_bulk_tool_failure_keeps_requested_ids_through_sdk(
    read_server: tuple[FastMCP, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed component reply fails the whole read, outside per-item handling."""
    server, client = read_server
    monkeypatch.setattr(
        search_state,
        "get_component_caps",
        AsyncMock(return_value=ComponentCaps(1, "test", frozenset({"states"}), {})),
    )
    ws = MagicMock(send_command=AsyncMock(return_value={"result": ["malformed"]}))
    monkeypatch.setattr(
        search_state, "get_websocket_client", AsyncMock(return_value=ws)
    )
    entity_ids = ["light.one", "sensor.two", "light.one"]

    async with Client(server) as sdk:
        result = await sdk.call_tool(
            "ha_get_state", {"entity_id": json.dumps(entity_ids)}, raise_on_error=False
        )

    assert result.is_error
    payload = json.loads(result.content[0].text)
    assert payload["success"] is False
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["entity_ids"] == entity_ids
    assert "metadata" not in payload
    ws.send_command.assert_awaited_once_with(
        "ha_mcp_tools/states", entity_ids=["light.one", "sensor.two"]
    )
    client.get_entity_state.assert_not_awaited()


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (RuntimeError("catalog unavailable"), "INTERNAL_ERROR"),
        (HomeAssistantAuthError("Invalid token"), "AUTH_INVALID_TOKEN"),
    ],
    ids=["unexpected-failure", "authentication-guidance"],
)
async def test_service_failure_retains_discovery_guidance_through_sdk(
    read_server: tuple[FastMCP, MagicMock], failure: Exception, code: str
) -> None:
    server, client = read_server
    client.get_services.side_effect = failure
    async with Client(server) as sdk:
        result = await sdk.call_tool("ha_list_services", {}, raise_on_error=False)

    assert result.is_error
    payload = json.loads(result.content[0].text)
    assert payload["success"] is False
    assert payload["error"]["code"] == code
    suggestions = payload["error"]["suggestions"]
    assert suggestions[-3:] == [
        "Check Home Assistant connection",
        "Verify WebSocket API is available",
        "Try with a specific domain filter",
    ]
    if isinstance(failure, HomeAssistantAuthError):
        assert "token" in suggestions[0].lower()
    client.get_services.assert_awaited_once_with()


async def test_single_state_failure_keeps_entity_and_search_guidance(
    read_server: tuple[FastMCP, MagicMock],
) -> None:
    server, client = read_server
    client.get_entity_state.side_effect = HomeAssistantAPIError(
        "missing", status_code=404
    )
    async with Client(server) as sdk:
        result = await sdk.call_tool(
            "ha_get_state", {"entity_id": "light.missing"}, raise_on_error=False
        )
    assert result.is_error
    payload = json.loads(result.content[0].text)
    error = payload["error"]
    assert error["code"] == "ENTITY_NOT_FOUND"
    assert payload["entity_id"] == "light.missing"
    assert "Use ha_search() to find correct entity IDs" in error["suggestions"]


async def test_tool_failure_envelope_is_not_classified_again(
    read_server: tuple[FastMCP, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, client = read_server
    payload = create_error_response(
        ErrorCode.VALIDATION_FAILED,
        "Catalog refused by its owner",
        context={"owner": "catalog"},
        suggestions=["Keep the owner's guidance"],
    )
    payload["warnings"] = ["Owner's additional detail"]
    client.get_services.side_effect = ToolFailure(
        "VALIDATION_FAILED", "Catalog refused by its owner", data=payload
    )
    classifier = MagicMock(side_effect=AssertionError("Already classified"))
    monkeypatch.setattr(helpers, "exception_to_structured_error", classifier)
    async with Client(server) as sdk:
        result = await sdk.call_tool("ha_list_services", {}, raise_on_error=False)
    assert result.is_error
    assert json.loads(result.content[0].text) == payload
    classifier.assert_not_called()


async def test_private_scene_failure_reaches_its_owner_unclassified() -> None:
    failure = SceneStorageConfigNotFoundError("scene.vendor", platform="hue")
    client = MagicMock(get_scene_config=AsyncMock(side_effect=failure))
    with pytest.raises(SceneStorageConfigNotFoundError) as caught:
        await ConfigSceneTools(client)._get_scene_config_internal("scene.vendor")
    assert caught.value is failure


async def test_legacy_context_is_injected_into_registered_backup_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neutral facade ctx remains SDK-injected and reaches the action dispatcher."""
    manage = AsyncMock(return_value={"backups": []})
    monkeypatch.setattr(BackupTools, "_manage", manage)
    server = FastMCP("legacy-context")
    register_backup_tools(server, MagicMock())
    async with Client(server) as sdk:
        listed = await sdk.list_tools()
        backup = next(tool for tool in listed if tool.name == "ha_manage_backup")
        assert "ctx" not in backup.inputSchema["properties"]
        assert {"scope", "action"} <= backup.inputSchema["properties"].keys()
        result = await sdk.call_tool(
            "ha_manage_backup", {"scope": "snapshot", "action": "list"}
        )
    assert not result.is_error
    manage.assert_awaited_once()
    assert isinstance(manage.await_args.kwargs["ctx"], Context)
