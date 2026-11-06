"""A locally refused admin request must not become an ambiguous HTTP write."""

from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.errors import HomeAssistantAdminRequiredError


async def test_local_admin_refusal_keeps_its_type_without_dispatch_or_replay():
    client = object.__new__(HomeAssistantClient)
    refusal = HomeAssistantAdminRequiredError("request was refused locally")
    client._is_admin = None
    client._admin_route_refused = False
    client._request = AsyncMock(side_effect=refusal)
    client._call_service_over_websocket = AsyncMock()
    with pytest.raises(HomeAssistantAdminRequiredError) as failure:
        await client._call_service_over_rest("script", "reload", {}, False)
    assert failure.value is refusal
    client._request.assert_awaited_once()
    client._call_service_over_websocket.assert_not_awaited()
    assert client._admin_route_refused is False
