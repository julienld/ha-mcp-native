"""The resource service facade keeps public metadata and legacy snapshot context."""

from types import SimpleNamespace
from unittest.mock import AsyncMock


async def test_legacy_resource_facade_retains_client_for_automatic_snapshot(
    monkeypatch,
):
    from ha_mcp.config import get_global_settings
    from ha_mcp.tools import auto_backup
    from ha_mcp.tools.tools_resources import ResourceTools

    settings = get_global_settings().model_copy(update={"enable_auto_backup": True})
    capture = AsyncMock()
    monkeypatch.setattr(auto_backup, "get_global_settings", lambda: settings)
    monkeypatch.setattr(
        auto_backup,
        "get_backup_manager",
        lambda *_: SimpleNamespace(maybe_snapshot=capture),
    )
    client = SimpleNamespace(
        send_websocket_message=AsyncMock(
            return_value={"success": True, "result": {"id": "existing"}}
        )
    )
    result = await ResourceTools(client).ha_config_set_dashboard_resource(
        url="/local/changed.js", resource_id="existing"
    )
    assert result["success"]
    assert capture.await_count == 1
    assert capture.call_args.args == ("dashboard_resource", "existing")
    assert capture.call_args.kwargs["tool_name"] == "ha_config_set_dashboard_resource"
