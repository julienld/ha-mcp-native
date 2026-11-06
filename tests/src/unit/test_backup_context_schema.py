"""Backup context is supplied by the server rather than accepted as public input."""

from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp import Client, Context, FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.backup import BackupTools, register_backup_tools


async def test_legacy_backup_schema_hides_context_and_injects_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FastMCP("backup context")
    manage = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(BackupTools, "_manage", manage)
    register_backup_tools(server, object())
    async with Client(server) as client:
        tools = await client.list_tools()
        assert "ctx" not in tools[0].inputSchema["properties"]
        with pytest.raises(ToolError, match="Unexpected keyword"):
            await client.call_tool(
                "ha_manage_backup",
                {"scope": "edits", "action": "list", "ctx": {"caller": "fake"}},
            )
        manage.assert_not_awaited()
        await client.call_tool("ha_manage_backup", {"scope": "edits", "action": "list"})
    assert isinstance(manage.await_args.kwargs["ctx"], Context)
