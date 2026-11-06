"""Missing remote configuration keeps useful, visibility-filtered suggestions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.backends.remote_config_ops import execute_config_operation
from ha_mcp.errors import HomeAssistantAPIError


@pytest.mark.parametrize("domain,bare", [("automation", False), ("script", True)])
async def test_config_not_found_uses_the_shared_visible_registry_hint(
    monkeypatch, domain, bare
):
    from ha_mcp.tools import util_helpers

    available = ["visible" if bare else "automation.visible"]
    listing = AsyncMock(return_value=available)
    monkeypatch.setattr(util_helpers, "list_visible_registry_ids", listing)
    client = SimpleNamespace(
        _resolve_script_id=AsyncMock(return_value="missing"),
        **{
            f"get_{domain}_config": AsyncMock(
                side_effect=HomeAssistantAPIError("not found", status_code=404)
            )
        },
    )
    result = await execute_config_operation(
        client, f"config.{domain}.read", {"identifier": "missing"}
    )
    assert result["found"] is False and result["available_ids"] == available
    listing.assert_awaited_once_with(client, domain, bare=bare)
