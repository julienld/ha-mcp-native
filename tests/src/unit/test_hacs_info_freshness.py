"""Repository info follows the provider endpoint rather than the cache bridge."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.runtime import hacs_remote


@pytest.mark.parametrize("domain", ["hacs", "marketplace"])
@pytest.mark.parametrize("identifier", ["42", "Example/Repo"])
async def test_info_uses_the_store_refresh_endpoint(domain, identifier):
    async def send(command, **arguments):
        if command.endswith("/repositories/list"):
            return {
                "success": True,
                "result": [{"id": "42", "full_name": "example/repo"}],
            }
        assert command == f"{domain}/repository/info"
        assert arguments == {"repository_id": "42"}
        return {
            "success": True,
            "result": {"id": "42", "additional_info": "Fresh README", "future": True},
        }

    ws = SimpleNamespace(send_command=AsyncMock(side_effect=send))
    row, metadata = await hacs_remote.repository_info(ws, domain, identifier)
    assert row["additional_info"] == "Fresh README" and row["future"] is True
    assert metadata["source"] == f"{domain}_info"
    assert all(
        call.args[0] != "ha_mcp_tools/hacs_cache"
        for call in ws.send_command.call_args_list
    )


async def test_info_failure_never_falls_back_or_replays():
    ws = SimpleNamespace(
        send_command=AsyncMock(
            side_effect=HomeAssistantCommandError("denied", "unauthorized")
        )
    )
    with pytest.raises(HomeAssistantCommandError):
        await hacs_remote.repository_info(ws, "hacs", "42")
    ws.send_command.assert_awaited_once_with("hacs/repository/info", repository_id="42")
