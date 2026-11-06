"""Remote store selection and read/write boundaries over a stubbed transport."""

import copy
from types import SimpleNamespace
from typing import Any
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
from ha_mcp.tools.tools_hacs import HacsTools, _assert_hacs_available


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
        info_replies = {
            "marketplace/info": state,
            "marketplace/repository/info": row,
            "get_states": [
                {
                    "entity_id": "update.user_renamed",
                    "attributes": {
                        "installed_version": row["installed_version"],
                        "latest_version": row["available_version"],
                        "in_progress": False,
                    },
                }
            ],
        }
        if name in info_replies:
            return {"success": True, "result": copy.deepcopy(info_replies[name])}
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


@pytest.mark.parametrize("domain", ["hacs", "marketplace"])
async def test_search_keeps_selected_store_info_and_filtered_catalog(
    remote, monkeypatch, domain
):
    """The selected owner's info reply survives, including unknown fields."""
    info = {
        "version": "selected-owner-build",
        "categories": ["plugin", "future-category"],
        "startup": False,
        "future_status": {"kept": True},
    }
    if domain == "marketplace":
        info.update(github_connected=False, warning_accepted=False)
    remote.row.update(category="plugin", future_catalog={"kept": True})

    async def send(name, **values):
        if name.endswith("/info"):
            if name == f"{domain}/info":
                return {"success": True, "result": copy.deepcopy(info)}
            raise HomeAssistantCommandError("Unknown command", "unknown_command")
        assert name == f"{domain}/repositories/list"
        assert values == {"categories": ["plugin"]}
        return {"success": True, "result": [copy.deepcopy(remote.row)]}

    remote.ws.send_command.side_effect = send
    monkeypatch.setattr(
        "ha_mcp.client.websocket_client.get_websocket_client",
        AsyncMock(return_value=remote.ws),
    )

    async def timezone(_client, data):
        return data

    monkeypatch.setattr("ha_mcp.tools.tools_hacs.add_timezone_metadata", timezone)
    client = SimpleNamespace(base_url="http://ha.test", token="test", verify_ssl=True)
    result = await HacsTools(client).ha_get_hacs_info(
        action="search", category="lovelace", installed_only=True
    )
    assert result["store"] == {"api": domain, **info}
    assert result["results"][0]["category"] == "lovelace"
    assert result["results"][0]["future_catalog"] == {"kept": True}
    assert result["count"] == 1 and result["category_filter"] == "lovelace"


async def test_remote_info_preserves_the_store_info_refresh_policy(remote):
    result = await remote.store.info("1")
    assert result["data"]["can_install"]
    assert result["data"]["integration_categories"] == ["hub"]
    remote.ws.send_command.assert_awaited_once_with(
        "marketplace/repository/info", repository_id="1"
    )


async def test_url_add_looks_up_the_registered_full_name(remote: Any) -> None:
    async def send(name: str, **values: Any) -> Any:
        if name == "ha_mcp_tools/hacs_cache":
            assert values["repository_id"] == "example/repository"
        return await remote.send(name, **values)

    remote.ws.send_command.side_effect = send
    result = await remote.store.write(
        "add_repository",
        repository="https://github.com/example/repository",
        category="integration",
    )
    assert result["repository_id"] == "1"


@pytest.mark.parametrize("version", ["main", "unconfirmed"])
async def test_default_branch_download_accepts_commit_readback_only_for_that_branch(
    remote: Any, version: str
) -> None:
    remote.row["default_branch"] = "main"

    async def send(name: str, **values: Any) -> Any:
        result = await remote.send(name, **values)
        if name == "call_service":
            remote.row["installed_version"] = "commit-sha"
        return result

    remote.ws.send_command.side_effect = send
    if version == "main":
        result = await remote.store.write("download", "1", version)
        assert result["installed_version"] == "commit-sha"
    else:
        with pytest.raises(ToolFailure) as error:
            await remote.store.write("download", "1", version)
        assert error.value.status == "outcome_unknown"


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
        await cached_repository(remote.ws, "marketplace", "1")
    remote.ws.send_command.assert_awaited_once_with(
        "ha_mcp_tools/hacs_cache", repository_id="1"
    )


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
    with pytest.raises(ToolFailure, match=r"warning_not_accepted.*first-run warning"):
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


@pytest.mark.parametrize("domain", ["hacs", "marketplace"])
@pytest.mark.parametrize("latest", [None, "1"])
async def test_versionless_reinstall_selects_store_before_any_service_dispatch(
    remote, domain, latest
):
    remote.row.update(
        installed_version="1", available_version=latest, pending_upgrade=True
    )

    async def send(name, **values):
        result = await remote.send(name, **values)
        if name == "config/entity_registry/list":
            result["result"][0]["platform"] = domain
        if name == "ha_mcp_tools/hacs_cache":
            result["result"]["store"] = domain
        return result

    remote.ws.send_command.side_effect = send
    if domain == "hacs":
        assert await try_hacs_update(remote.ws, "1", None) is None
    else:
        await remote.store.write("download", "1")
    assert not any(
        c.args[0] == "call_service" for c in remote.ws.send_command.call_args_list
    )
    if domain == "marketplace":
        remote.ws.send_command.assert_any_await(
            "marketplace/repository/install", repository="1", _wait_timeout=120.0
        )


async def test_admitted_store_invalid_identifier_names_supported_formats(remote):
    with pytest.raises(ToolFailure) as refused:
        await remote.store.write(
            "add_repository",
            repository="invalid-format-no-slash",
            category="integration",
        )
    assert refused.value.status == "not_started"
    assert refused.value.code == "VALIDATION_INVALID_PARAMETER"
    message = str(refused.value)
    assert (
        "format" in message.lower() and "owner/repo" in message and "HTTP(S)" in message
    )
    remote.ws.send_command.assert_awaited_once_with("marketplace/info")
