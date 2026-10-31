"""Public MCP projection keeps errors honest and never uses a client's host."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp import self_update
from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp.tools.tools_self_update import register_self_update_tools


async def test_public_tool_returns_manual_owner_path_as_an_error_without_remote_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        self_update,
        "_local_evidence",
        lambda: self_update.InstallationEvidence(
            container=True, distributions=("ha-mcp",), installer="pip"
        ),
    )
    ha_client = AsyncMock()
    server = FastMCP("Self update contract")
    register_self_update_tools(server, ha_client)
    async with Client(server) as client:
        tools = await client.list_tools()
        advertised = next(
            tool for tool in tools if tool.name == "ha_manage_ha_mcp_updates"
        )
        assert advertised.annotations.readOnlyHint is False
        assert advertised.annotations.destructiveHint is True
        assert advertised.annotations.idempotentHint is False
        assert advertised.annotations.openWorldHint is True
        guide = await client.call_tool("ha_manage_ha_mcp_updates", {"action": "guide"})
        data: Any = guide.structured_content
        assert data["installation"]["method"] == "docker"
        assert data["capability"] == "manual_required"
        update = await client.call_tool(
            "ha_manage_ha_mcp_updates", {"action": "update"}, raise_on_error=False
        )
        assert update.is_error
        assert "External installation owner required" in update.content[0].text
    assert not ha_client.mock_calls


async def test_direct_reference_guide_never_suggests_switching_to_pypi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        self_update,
        "_local_evidence",
        lambda: self_update.InstallationEvidence(
            distributions=("ha-mcp",), installer="pip", direct_reference=True
        ),
    )
    result = await self_update.ProcessSelfUpdate().manage("guide")
    steps = " ".join(result["guide"]["steps"])
    assert "same origin" in steps
    assert "pip install --upgrade" not in steps


async def test_direct_reference_check_does_not_present_pypi_as_its_update_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp import update_check

    monkeypatch.setattr(
        self_update,
        "_local_evidence",
        lambda: self_update.InstallationEvidence(
            distributions=("ha-mcp",), installer="pip", direct_reference=True
        ),
    )
    notice = AsyncMock()
    monkeypatch.setattr(update_check, "get_update_field", notice)
    result = await self_update.ProcessSelfUpdate().manage("check")
    assert result["available_version"] is None
    notice.assert_not_awaited()
