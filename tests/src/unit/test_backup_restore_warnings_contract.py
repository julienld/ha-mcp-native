"""Full-backup recovery keeps warnings in the established public shape."""

from unittest.mock import AsyncMock, MagicMock, patch

from .test_helper_response_shape import _assert_warnings_list_shape


async def test_backup_restore_warnings_shape():
    from ha_mcp.tools.backup import restore_backup

    ws = AsyncMock()
    ws.send_command.side_effect = [
        {"success": True, "result": {"backups": [{"backup_id": "abc"}]}},
        {
            "success": True,
            "result": {"agents": [{"agent_id": "backup.local", "name": "local"}]},
        },
        {
            "success": True,
            "result": {"config": {"create_backup": {"password": "pw"}}},
        },
        {"success": True, "result": {"backup_job_id": "job"}},
        {"success": True},
    ]
    client = MagicMock()
    client.base_url = "http://test"
    client.token = "t"
    client.verify_ssl = False
    # Stub the safety-backup completion poll (exercised by test_backup_restore)
    # so this test stays focused on the warnings-list shape contract.
    with (
        patch(
            "ha_mcp.tools.backup.get_component_caps",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ha_mcp.tools.backup.get_connected_ws_client",
            new=AsyncMock(return_value=(ws, None)),
        ),
        patch(
            "ha_mcp.tools.backup._poll_backup_completion",
            new=AsyncMock(return_value={"success": True}),
        ),
    ):
        result = await restore_backup(client, "abc")
    _assert_warnings_list_shape(result)
