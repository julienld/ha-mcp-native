"""The HACS tools on Home Assistant 2026.11+, where HACS is the Marketplace.

Core 2026.11 removes the ``hacs/*`` WebSocket commands and serves
``marketplace/*`` instead; the tools must pick whichever answers and keep
the store's own data (categories, repository fields, refusals) intact.
"""

import copy
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.runtime.hacs_remote import MarketplaceRemote, _repository_full_name
from ha_mcp.tool_api import ToolFailure
from ha_mcp.tools.hacs_backend import StoreApi, detect_store_api
from ha_mcp.tools.hacs_registration import wait_for_repo_registration
from ha_mcp.tools.tools_hacs import HacsTools

MARKETPLACE = StoreApi(
    "marketplace",
    {
        "categories": ["integration", "plugin", "template", "theme"],
        "warning_accepted": True,
        "github_connected": False,
    },
)


async def _identity_timezone(_client, data):
    return data


def _ws(*replies):
    ws = AsyncMock()
    ws.send_command = AsyncMock(side_effect=list(replies))
    return ws


def _ok(result):
    return {"success": True, "result": result}


@contextmanager
def _on_marketplace(ws, domain="marketplace"):
    with (
        patch(
            "ha_mcp.tools.tools_hacs._assert_hacs_available",
            new=AsyncMock(return_value=domain),
        ),
        patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws),
        ),
        patch("ha_mcp.tools.tools_hacs.add_timezone_metadata", new=_identity_timezone),
    ):
        yield


@pytest.fixture
def tools():
    return HacsTools(MagicMock())


class TestDetectStoreApi:
    async def test_core_2026_11_is_served_by_the_marketplace(self):
        ws = _ws(_ok({"categories": ["integration"], "warning_accepted": False}))
        api = await detect_store_api(ws)
        assert api.is_marketplace
        assert api.info["warning_accepted"] is False
        ws.send_command.assert_awaited_once_with("marketplace/info")

    async def test_older_core_falls_back_to_the_hacs_integration(self):
        ws = _ws(
            HomeAssistantCommandError(
                "Command failed: Unknown command.", "unknown_command"
            ),
            _ok({"categories": ["integration", "appdaemon"]}),
        )
        api = await detect_store_api(ws)
        assert not api.is_marketplace
        assert api.command("install") == "hacs/repository/download"

    async def test_unloaded_marketplace_is_not_reported_as_missing_hacs(self):
        ws = _ws(HomeAssistantCommandError("not loaded", "not_loaded"))
        with pytest.raises(ToolError) as excinfo:
            await detect_store_api(ws)
        assert "COMPONENT_NOT_INSTALLED" in str(excinfo.value)
        assert "Marketplace" in str(excinfo.value)
        ws.send_command.assert_awaited_once()

    async def test_neither_store_names_both_options(self):
        unknown = HomeAssistantCommandError("Unknown command.", "unknown_command")
        ws = _ws(unknown, unknown)
        with pytest.raises(ToolError) as excinfo:
            await detect_store_api(ws)
        assert "COMPONENT_NOT_INSTALLED" in str(excinfo.value)
        assert "2026.11" in str(excinfo.value)


def _store(*, row=None, info=None, domain="marketplace"):
    repository = {
        "id": "123",
        "full_name": "o/r",
        "category": "integration",
        "installed": False,
        **(row or {}),
    }
    state = {**MARKETPLACE.info, **(info or {})}
    world = SimpleNamespace(
        row=repository,
        info=state,
        failure=None,
        releases=AsyncMock(return_value=[]),
        notes=None,
    )

    async def send(name, **values):
        info_replies = {
            f"{domain}/info": state,
            f"{domain}/repository/info": repository,
        }
        if name in info_replies:
            result = info_replies[name]
        elif name == "ha_mcp_tools/hacs_cache":
            result = {
                "store": domain,
                "repository": repository,
                "cache": {"source": f"{domain}_cache", "refreshed_from_github": False},
            }
        elif name == f"{domain}/repositories/list":
            result = [repository]
        elif name == "config/entity_registry/list":
            result = []
        elif name == f"{domain}/repository/releases":
            result = await world.releases()
        elif name == "update/release_notes":
            result = world.notes
        elif name in {
            f"{domain}/repository/install",
            f"{domain}/repository/download",
            f"{domain}/repository/uninstall",
            f"{domain}/repositories/add",
        }:
            if world.failure:
                raise world.failure
            repository["installed"] = not name.endswith("/uninstall")
            repository["installed_version"] = values.get("version", "latest")
            repository["category"] = values.get("category", repository["category"])
            result = None
        else:
            raise AssertionError(f"Unexpected command: {name}")
        return _ok(copy.deepcopy(result))

    world.ws = SimpleNamespace(send_command=AsyncMock(side_effect=send))
    return world


def _writes(ws):
    return [
        call
        for call in ws.send_command.await_args_list
        if call.args[0].endswith(("/install", "/download", "/uninstall", "/add"))
    ]


class TestMarketplaceCommands:
    async def test_download_installs_through_the_marketplace(self, tools):
        world = _store()
        with _on_marketplace(world.ws):
            result = await tools.ha_manage_hacs(
                action="download", repository_id="123", version="v2.0.0"
            )
        assert result["installed_version"] == "v2.0.0"
        writes = _writes(world.ws)
        assert len(writes) == 1
        assert writes[0].args == ("marketplace/repository/install",)
        assert writes[0].kwargs == {
            "repository": "123",
            "version": "v2.0.0",
            "_wait_timeout": 120.0,
        }

    async def test_remove_uninstalls_through_the_marketplace(self, tools):
        world = _store(row={"installed": True})
        with _on_marketplace(world.ws):
            await tools.ha_manage_hacs(action="remove", repository_id="123")
        writes = _writes(world.ws)
        assert len(writes) == 1
        assert writes[0].args == ("marketplace/repository/uninstall",)
        assert world.row["installed"] is False

    async def test_unaccepted_warning_tells_the_user_where_to_accept_it(self, tools):
        world = _store(info={"warning_accepted": False})
        with _on_marketplace(world.ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="download", repository_id="123")
        text = str(excinfo.value)
        assert "AUTH_FORBIDDEN" in text
        assert "warning_not_accepted" in text
        assert "first-run warning" in text
        assert "Marketplace UI" in text
        assert "cannot accept" in text
        assert not _writes(world.ws)

    async def test_github_gate_is_not_mistaken_for_a_connection_failure(self, tools):
        world = _store(info={"github_connected": False})
        with _on_marketplace(world.ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="add_repository", repository="o/r", category="integration"
            )
        text = str(excinfo.value)
        assert "AUTH_FORBIDDEN" in text and "CONNECTION_FAILED" not in text
        assert "connect GitHub" in text
        assert not _writes(world.ws)

    async def test_category_is_checked_against_the_store_list(self, tools):
        world = _store(info={"categories": ["template", "theme"]})
        with _on_marketplace(world.ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_get_hacs_info(action="search", category="integration")
        assert "VALIDATION_INVALID_PARAMETER" in str(excinfo.value)
        assert "template" in str(excinfo.value)
        assert not any(
            call.args[0] == "marketplace/repositories/list"
            for call in world.ws.send_command.await_args_list
        )

    async def test_category_case_is_the_stores_to_fold(self, tools):
        world = _store()
        with _on_marketplace(world.ws):
            await tools.ha_get_hacs_info(action="search", category="Integration")
        world.ws.send_command.assert_any_await(
            "marketplace/repositories/list", categories=["integration"]
        )

    async def test_add_folds_category_before_confirming_the_registered_repository(self):
        world = _store(info={"github_connected": True})
        result = await MarketplaceRemote(world.ws).write(
            "add_repository", repository="o/r", category="Integration"
        )
        assert result["repository_id"] == "123"
        writes = _writes(world.ws)
        assert len(writes) == 1
        assert writes[0].kwargs["category"] == "integration"

    async def test_add_refuses_an_unoffered_category_before_dispatch(self):
        world = _store(info={"github_connected": True, "categories": ["theme"]})
        with pytest.raises(ToolError) as error:
            await MarketplaceRemote(world.ws).write(
                "add_repository", repository="o/r", category="integration"
            )
        assert "VALIDATION_INVALID_PARAMETER" in str(error.value)
        assert not _writes(world.ws)

    async def test_built_in_replacement_names_the_confirmation(self, tools):
        world = _store()
        world.failure = HomeAssistantCommandError(
            "Command failed: o/youtube replaces the built-in youtube integration; "
            "confirm that before installing it",
            "replaces_built_in",
        )
        with _on_marketplace(world.ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="download", repository_id="123")
        text = str(excinfo.value)
        assert "confirm_replace_built_in=True" in text
        assert "OUTCOME_UNKNOWN" in text
        assert len(_writes(world.ws)) == 1

    @pytest.mark.parametrize("domain", ["marketplace", "hacs"])
    async def test_confirmed_replacement_reaches_only_the_marketplace(
        self, tools, domain
    ):
        world = _store(domain=domain)
        with _on_marketplace(world.ws, domain):
            await tools.ha_manage_hacs(
                action="download", repository_id="123", confirm_replace_built_in=True
            )
        writes = _writes(world.ws)
        assert len(writes) == 1
        assert ("confirm_replace_built_in" in writes[0].kwargs) is (
            domain == "marketplace"
        )

    async def test_lovelace_alias_reaches_the_store_as_plugin(self, tools):
        world = _store()
        with _on_marketplace(world.ws):
            await tools.ha_get_hacs_info(action="search", category="lovelace")
        world.ws.send_command.assert_any_await(
            "marketplace/repositories/list", categories=["plugin"]
        )


class TestNativeFieldsReachTheCaller:
    async def test_search_rows_and_store_status_are_the_stores_own(self, tools):
        row = {
            "id": "1",
            "name": "Card",
            "full_name": "o/card",
            "category": "plugin",
            "installed": True,
            "can_install": True,
            "local_path": "/config/www/community/card",
        }
        world = _store(row=row)
        with _on_marketplace(world.ws):
            result = await tools.ha_get_hacs_info(action="search", installed_only=True)
        assert result["results"] == [
            {
                **row,
                "category": "lovelace",
                "description": None,
                "stars": 0,
                "downloads": 0,
                "authors": [],
                "installed_version": None,
                "available_version": None,
                "pending_update": False,
                "domain": None,
            }
        ]
        assert result["store"] == {"api": "marketplace", **world.info}

    async def test_info_keeps_every_field_and_the_readme(self, tools):
        detail = {"id": "1", "additional_info": "# README", "replaces_built_in": None}
        world = _store(row=detail)
        with _on_marketplace(world.ws):
            result = await tools.ha_get_hacs_info(action="info", repository_id="1")
        assert result["data"]["additional_info"] == "# README"
        assert "replaces_built_in" in result["data"]
        assert "readme_note" in result
        assert any(
            call.args[0] == "marketplace/repository/info"
            for call in world.ws.send_command.await_args_list
        )

    async def test_a_slow_releases_fetch_leaves_the_info_and_warns(self, tools):
        world = _store(row={"id": "1", "pending_upgrade": False})
        world.releases.side_effect = HomeAssistantCommandTimeout("timed out")
        with _on_marketplace(world.ws):
            out = await tools.ha_get_hacs_info(
                action="info", repository_id="1", include_releases=True
            )
        assert out["data"]["full_name"] == "o/r"
        assert "warnings" not in out["data"]
        assert out["warnings"] == ["Releases unavailable: timed out"]
        world.ws.send_command.assert_any_await(
            "marketplace/repository/releases", repository_id="1", _wait_timeout=60.0
        )

    async def test_release_notes_come_from_the_update_entity(self, tools):
        world = _store(
            row={
                "id": "1",
                "pending_upgrade": True,
                "update_entity_id": "update.card_update",
            }
        )
        world.releases.return_value = [{"tag": "v2", "prerelease": False}]
        world.notes = "## v2\nFixes"
        with _on_marketplace(world.ws):
            result = await tools.ha_get_hacs_info(
                action="info", repository_id="1", include_releases=True
            )
        assert result["github_releases"] == world.releases.return_value
        assert result["release_notes"] == world.notes
        world.ws.send_command.assert_any_await(
            "update/release_notes", entity_id="update.card_update", _wait_timeout=60.0
        )


async def test_marketplace_registration_is_one_list_lookup():
    ws = _ws(_ok([{"id": "9", "full_name": "Owner/Repo"}]))
    ws.subscribe_command = AsyncMock()
    repo = await wait_for_repo_registration(
        ws, "owner/repo", timeout=10.0, api=MARKETPLACE
    )
    assert repo == {"id": "9", "full_name": "Owner/Repo"}
    ws.subscribe_command.assert_not_awaited()
    ws.send_command.assert_awaited_once_with("marketplace/repositories/list")


@pytest.mark.parametrize(
    "repository",
    [
        "Owner/Repo",
        "https://github.com/Owner/Repo.git/",
        "github.com/Owner/Repo",
        "//github.com/Owner/Repo",
        "https://GITHUB.COM/Owner/Repo?tab=readme#section",
    ],
)
def test_repository_identity_accepts_github_urls_and_bare_names(repository):
    assert _repository_full_name(repository) == "owner/repo"


@pytest.mark.parametrize(
    "repository",
    [
        "https://github.com.evil.test/owner/repo",
        "https://evil.test/github.com/owner/repo",
        "https://github.com@evil.test/owner/repo",
        "https://evil.test@github.com/owner/repo",
        "ftp://github.com/owner/repo",
        "owner",
        "owner/../repo",
    ],
)
async def test_invalid_repository_url_never_dispatches_add_or_replays(repository):
    world = _store(info={"github_connected": True})
    with pytest.raises(ToolFailure) as error:
        await MarketplaceRemote(world.ws).write(
            "add_repository", repository=repository, category="integration"
        )
    assert error.value.code == "VALIDATION_INVALID_PARAMETER"
    assert error.value.status == "not_started"
    assert not _writes(world.ws)
