"""The common engine also invokes the existing remote clients and schemas."""

from unittest.mock import AsyncMock

import pytest

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import build_initial_catalog
from ha_mcp.tool_api import CallContext
from ha_mcp.tools import component_api, tools_service

from ._component_routing_helpers import make_ws, patch_ws
from .test_ha_call_service_component_routing import (
    _CAPS_CALL,
    _CAPS_NONE,
    RoutingClient,
    _build_call_service,
    _confirmed_result,
)


@pytest.mark.parametrize("component", [True, False])
async def test_same_service_result_through_legacy_and_shared_engine(
    component: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    component_api._CAPS_CACHE.clear()
    monkeypatch.setattr(
        tools_service,
        "wait_for_state_change",
        AsyncMock(return_value={"entity_id": "light.a", "state": "on"}),
    )
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL if component else _CAPS_NONE,
        cmd_result=_confirmed_result(),
    )
    client = RoutingClient()
    backend = RemoteBackend(client)
    engine = Engine(backend, build_initial_catalog(backend))
    arguments = {"domain": "light", "service": "turn_on", "entity_id": "light.a"}
    with patch_ws(ws, tools_service):
        expected = await _build_call_service(client)(**arguments)
        actual = await engine.call("ha_call_service", arguments, CallContext("remote"))
    assert not actual.is_error
    assert actual.data == expected
    assert len(client.call_service_calls) == (0 if component else 2)


async def test_remote_keeps_explicit_arbitrary_ws_compatibility() -> None:
    client = RoutingClient()
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": {"issues": []}}
    )
    backend = RemoteBackend(client)
    result = await Engine(backend, build_initial_catalog(backend)).call(
        "ha_call_service", {"ws_command": "repairs/list_issues"}, CallContext("remote")
    )
    assert not result.is_error
    assert result.data["result"] == {"issues": []}
    client.send_websocket_message.assert_awaited_once_with(
        {"type": "repairs/list_issues"}
    )


async def test_remote_connection_error_preserves_public_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.client.rest_client import HomeAssistantConnectionError

    client = RoutingClient()
    client.call_service = AsyncMock(side_effect=HomeAssistantConnectionError("lost"))
    backend = RemoteBackend(client)
    result = await Engine(backend, build_initial_catalog(backend)).call(
        "ha_call_service",
        {"domain": "scene", "service": "turn_on"},
        CallContext("remote"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["error"]["code"] == "CONNECTION_FAILED"
    assert client.call_service.await_count == 1
