"""Event uncertainty preserves the shared warnings list contract."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.tools_service import ServiceTools

from .test_helper_response_shape import _assert_warnings_list_shape


@pytest.mark.asyncio
async def test_service_call_event_timeout_warnings_shape(monkeypatch):
    monkeypatch.setattr(
        "ha_mcp.tools.event_call.get_component_caps", AsyncMock(return_value=None)
    )
    error = HomeAssistantConnectionError("timeout")
    error.__cause__ = httpx.TimeoutException("read timed out")
    client = MagicMock()
    client._request = AsyncMock(return_value=[])
    client.send_websocket_message = AsyncMock(side_effect=error)
    tools = ServiceTools(client, MagicMock())
    result = await tools.ha_call_event("evt_x")
    assert result["partial"] is True
    assert client.send_websocket_message.await_count == 1
    _assert_warnings_list_shape(result)
