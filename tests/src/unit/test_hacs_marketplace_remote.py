"""Remote store selection and read/write boundaries over a stubbed transport."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.runtime.hacs_remote import (
    MarketplaceRemote,
    cached_repository,
    try_hacs_update,
)
from ha_mcp.tool_api import ToolFailure
from ha_mcp.tools.tools_hacs import _assert_hacs_available


@pytest.fixture
def remote():
    row = {
        "id": "1",
        "full_name": "example/repository",
        "category": "integration",
        "domain": "fixture",
        "name": "Fixture",
        "installed": True,
        "installed_version": "1",
        "available_version": "2",
        "can_install": True,
        "integration_categories": ["hub"],
    }
    state = {"warning_accepted": True, "github_connected": True, "startup": False}

    async def send(name, **values):
        if name == "marketplace/info":
            return {"success": True, "result": copy.deepcopy(state)}
        if name == "hacs/info":
            raise HomeAssistantCommandError("Unknown command", "unknown_command")
        if name == "ha_mcp_tools/hacs_cache":
            return {
                "success": True,
                "result": {
                    "store": "marketplace",
                    "repository": copy.deepcopy(row),
                    "cache": {
                        "source": "marketplace_cache",
                        "refreshed_from_github": False,
                    },
                },
            }
        if name == "marketplace/repositories/list":
            return {"success": True, "result": [copy.deepcopy(row)]}
        if name == "config/entity_registry/list":
            return {
                "success": True,
                "result": [
                    {
                        "entity_id": "update.user_renamed",
                        "platform": "marketplace",
                        "unique_id": "1",
                        "config_entry_id": "marketplace-entry",
                    }
                ],
            }
        if name == "call_service":
            row["installed_version"] = values["service_data"].get("version", "2")
        elif name == "marketplace/repository/install":
            row["installed_version"] = values.get("version", "2")
            row["installed"] = True
        elif name == "marketplace/repository/uninstall":
            row["installed"] = False
        return {"success": True, "result": None}

    ws = SimpleNamespace(send_command=AsyncMock(side_effect=send))
    return SimpleNamespace(
        ws=ws, state=state, row=row, send=send, store=MarketplaceRemote(ws)
    )


async def test_marketplace_selection_only_follows_explicit_absence(remote):
    assert await _assert_hacs_available(remote.ws) == "marketplace"


@pytest.mark.parametrize(
    "error",
    [
        HomeAssistantCommandTimeout("lost"),
        HomeAssistantCommandError("Forbidden", "unauthorized"),
    ],
)
async def test_selection_never_masks_transport_or_auth_failure(remote, error):
    remote.ws.send_command.side_effect = error
    with pytest.raises(type(error)):
        await _assert_hacs_available(remote.ws)
    remote.ws.send_command.assert_awaited_once_with("hacs/info")


async def test_multiple_remote_store_owners_refuse_selection(remote):
    remote.ws.send_command.side_effect = None
    remote.ws.send_command.return_value = {"success": True, "result": {}}
    with pytest.raises(ToolFailure, match="Both HACS and Marketplace"):
        await _assert_hacs_available(remote.ws)


async def test_remote_info_never_calls_mutating_info_endpoint(remote):
    result = await remote.store.info("1")
    assert result["data"]["can_install"]
    assert result["data"]["integration_categories"] == ["hub"]
    remote.ws.send_command.assert_awaited_once_with(
        "ha_mcp_tools/hacs_cache", repository_id="1"
    )


async def test_only_unknown_cache_command_selects_incomplete_catalog(remote):
    async def send(name, **values):
        if name == "ha_mcp_tools/hacs_cache":
            raise HomeAssistantCommandError("Unknown", "unknown_command")
        return await remote.send(name, **values)

    remote.ws.send_command.side_effect = send
    row, cache = await cached_repository(remote.ws, "marketplace", "example/repository")
    assert row["full_name"] == "example/repository"
    assert (
        cache["details_complete"] is False and cache["refreshed_from_github"] is False
    )
    assert [call.args[0] for call in remote.ws.send_command.await_args_list] == [
        "ha_mcp_tools/hacs_cache",
        "marketplace/repositories/list",
    ]


@pytest.mark.parametrize("code", ["unauthorized", "not_loaded", "unknown_error"])
async def test_cache_failure_does_not_fall_back(remote, code):
    remote.ws.send_command.side_effect = HomeAssistantCommandError("refused", code)
    with pytest.raises(HomeAssistantCommandError):
        await remote.store.info("1")
    remote.ws.send_command.assert_awaited_once()


async def test_remote_update_uses_renamed_entity_and_core_service(remote):
    result = await remote.store.write("download", "1", "2")
    assert (
        result["installed_version"] == "2"
        and result["active_version_verified"] is False
    )
    mutation = [
        call
        for call in remote.ws.send_command.await_args_list
        if call.args[0] == "call_service"
    ]
    assert len(mutation) == 1
    assert mutation[0].kwargs["service_data"] == {
        "entity_id": "update.user_renamed",
        "version": "2",
    }
    assert not any(
        call.args[0] == "marketplace/repository/install"
        for call in remote.ws.send_command.await_args_list
    )


@pytest.mark.parametrize("action", ["download", "add_repository"])
async def test_remote_consent_refusal_precedes_mutation(remote, action):
    remote.state["warning_accepted"] = False
    with pytest.raises(ToolFailure, match="warning_not_accepted"):
        await remote.store.write(
            action, "1", repository="example/new", category="integration"
        )
    remote.ws.send_command.assert_awaited_once_with("marketplace/info")


async def test_remote_update_loss_is_unknown_and_never_replayed(remote):
    async def send(name, **values):
        if name == "call_service":
            raise HomeAssistantCommandTimeout("after dispatch")
        return await remote.send(name, **values)

    remote.ws.send_command.side_effect = send
    with pytest.raises(ToolFailure) as error:
        await remote.store.write("download", "1")
    assert error.value.status == "outcome_unknown"
    calls = [call.args[0] for call in remote.ws.send_command.await_args_list]
    assert calls.count("call_service") == 1
    assert "marketplace/repository/install" not in calls


@pytest.mark.parametrize("failed", [False, True])
async def test_hacs_remote_prefers_standard_service_and_never_replays(remote, failed):
    async def send(name, **values):
        if failed and name == "call_service":
            raise HomeAssistantCommandTimeout("dispatched")
        result = await remote.send(name, **values)
        if name == "ha_mcp_tools/hacs_cache":
            result["result"]["store"] = "hacs"
        elif name == "config/entity_registry/list":
            result["result"][0]["platform"] = "hacs"
        return result

    remote.ws.send_command.side_effect = send
    if failed:
        with pytest.raises(ToolFailure) as error:
            await try_hacs_update(remote.ws, "1", "2")
        assert error.value.status == "outcome_unknown"
    else:
        result = await try_hacs_update(remote.ws, "1", "2")
        assert result["store"] == "hacs" and result["installed_version"] == "2"
    calls = [call.args[0] for call in remote.ws.send_command.await_args_list]
    assert calls.count("call_service") == 1
    assert "hacs/repository/download" not in calls


async def test_remote_update_entity_replacement_before_dispatch_is_refused(remote):
    reads = 0

    async def send(name, **values):
        nonlocal reads
        result = await remote.send(name, **values)
        if name == "config/entity_registry/list":
            reads += 1
            if reads > 1:
                result["result"][0]["config_entry_id"] = "other-entry"
        return result

    remote.ws.send_command.side_effect = send
    with pytest.raises(ToolFailure) as error:
        await remote.store.write("download", "1")
    assert error.value.status == "not_started"
    assert all(
        call.args[0] != "call_service"
        for call in remote.ws.send_command.await_args_list
    )
