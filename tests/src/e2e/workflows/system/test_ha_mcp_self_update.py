"""Read the public self-update tool in every isolated supported backend lane.

Never install a release in the shared test server: owner mutations and failure
receipts are tested in the dedicated unit world, real installation remains a
separate disposable-host qualification.
"""

from typing import Any

from ...utilities.assertions import MCPAssertions


async def test_self_update_guide_reports_owner_limits_without_mutating(
    mcp_client: Any,
) -> None:
    async with MCPAssertions(mcp_client) as mcp:
        result = await mcp.call_tool_success(
            "ha_manage_ha_mcp_updates", {"action": "guide"}
        )
    assert result["installation"]["method"] in {
        "component",
        "app",
        "docker",
        "pip",
        "uv",
        "uvx",
        "pipx",
        "source",
        "unknown",
    }
    assert result["capability"] in {"install", "manual_required"}
    assert "client configuration" in result["guide"]["preserve"]
    assert result["status"] in {"not_started", "checked", "pending_restart"}
