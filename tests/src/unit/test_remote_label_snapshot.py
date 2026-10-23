"""The shared label reader must still reach the retained remote registry owner."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime.labels import LabelDiscoveryTools
from ha_mcp.tool_api import CallContext


async def test_remote_label_discovery_survives_the_label_facade_migration():
    labels = [{"label_id": "kitchen", "name": "Kitchen", "future_field": False}]
    client = SimpleNamespace(
        send_websocket_message=AsyncMock(
            return_value={"success": True, "result": labels}
        )
    )
    tools = LabelDiscoveryTools(RemoteBackend(client), CallContext(None))

    result = await tools.ha_config_get_label()

    assert result["labels"] == labels
    client.send_websocket_message.assert_awaited_once_with(
        {"type": "config/label_registry/list"}
    )
