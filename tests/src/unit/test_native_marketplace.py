"""Marketplace adapter boundaries; these stand-ins are not a live Core qualification."""

import asyncio
import copy
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native
from .test_native_hacs import call
from .test_native_hacs import hacs as hacs


@pytest.fixture
def marketplace(hacs, monkeypatch):
    hacs.hass.config.components.remove("hacs")
    hacs.hass.config.components.add("marketplace")
    del hacs.hass.data["hacs"]
    hacs.entry.domain = "marketplace"
    hacs.entry.runtime_data = hacs.owner
    hacs.hass.config_entries.async_loaded_entries = lambda domain: (
        [hacs.entry] if domain == "marketplace" else []
    )
    hacs.hass.config_entries.async_entries = lambda domain: []
    hacs.owner.version = "2026.11.0.dev0"
    hacs.owner.warning_accepted = Mock(return_value=True)
    hacs.owner.github_connected = True
    hacs.owner.data_client = object()
    del hacs.owner.github
    del hacs.owner.system.running
    hacs.owner.system.removed = False
    import importlib

    access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.marketplace_access"
    )
    monkeypatch.setattr(access, "ConfigEntryState", hacs.access.ConfigEntryState)
    hacs.repo.marketplace = hacs.owner
    del hacs.repo.hacs
    hacs.repo.can_install = True
    hacs.repo.installing = False
    hacs.repo.async_install_repository = AsyncMock(side_effect=hacs.repo._download)
    del hacs.repo.ignored_by_country_configuration
    hacs.hass.data["websocket_api"] = {
        name.replace("hacs/", "marketplace/")
        .replace("/download", "/install")
        .replace("/remove", "/uninstall"): registration
        for name, registration in hacs.hass.data["websocket_api"].items()
    }
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.marketplace.base",
        SimpleNamespace(async_get_marketplace=lambda hass: hacs.entry.runtime_data),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.marketplace.utils.regex",
        SimpleNamespace(extract_repository_from_url=lambda value: value),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.marketplace.websocket.repositories",
        SimpleNamespace(
            repository_summary=lambda owner, repo: {
                **repo.data.to_json(),
                "name": repo.display_name,
                "can_install": repo.can_install,
                "integration_categories": ["hub"],
                "available_version": repo.display_available_version,
                "installed_version": repo.display_installed_version,
                "pending_upgrade": repo.pending_update,
                "stars": repo.data.stargazers_count,
                "future_core_field": {"kept": True},
            }
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.marketplace.const",
        SimpleNamespace(SIGNAL_REPOSITORY_INSTALLED="installed"),
    )
    hacs.signal = Mock()
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.dispatcher",
        SimpleNamespace(async_dispatcher_send=hacs.signal),
    )
    original_register = hacs.owner.async_register_repository.side_effect

    async def register(**kwargs):
        await original_register(**kwargs)
        hacs.repositories["2"].marketplace = hacs.owner
        hacs.repositories["2"].can_install = True

    hacs.owner.async_register_repository.side_effect = register
    return hacs


async def test_marketplace_cached_info_preserves_core_fields_without_mutation(
    marketplace,
):
    marketplace.repo.data.last_fetched = None
    result = await call(marketplace, "info", repository_id="1")
    assert not result.is_error, result.data
    data = result.data["data"]
    assert data["data"]["can_install"] is True
    assert data["data"]["future_core_field"] == {"kept": True}
    assert data["data"]["integration_categories"] == ["hub"]
    assert data["cache"]["source"] == "marketplace_cache"
    assert not data["cache"]["refreshed_from_github"]
    assert marketplace.repo.data.new
    marketplace.repo.update_repository.assert_not_awaited()
    marketplace.owner.data.async_write.assert_not_awaited()


async def test_marketplace_search_keeps_native_catalog_metadata(marketplace):
    result = await call(marketplace, "search")
    assert not result.is_error, result.data
    row = result.data["data"]["results"][0]
    assert row["future_core_field"] == {"kept": True}
    assert row["can_install"] and row["integration_categories"] == ["hub"]


@pytest.mark.parametrize("action", ["download", "add_repository"])
async def test_marketplace_consent_is_required_before_mutation(marketplace, action):
    marketplace.owner.warning_accepted.return_value = False
    args = (
        {"repository": "example/new", "category": "integration"}
        if action == "add_repository"
        else {"repository_id": "1"}
    )
    result = await call(marketplace, action, **args)
    assert result.is_error and result.status == "not_started", result.data
    assert "warning" in str(result.data).lower()
    marketplace.repo.async_install_repository.assert_not_awaited()
    marketplace.owner.async_register_repository.assert_not_awaited()


async def test_two_loaded_store_owners_refuse_selection(marketplace):
    marketplace.hass.config.components.add("hacs")
    marketplace.hass.data["hacs"] = copy.copy(marketplace.owner)
    result = await call(marketplace, "download", repository_id="1")
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "CONFIG_CONFLICT"


@pytest.mark.parametrize("category", ["appdaemon", "python_script"])
async def test_removed_marketplace_categories_are_explicitly_refused(
    marketplace, category
):
    result = await call(marketplace, "search", category=category)
    assert result.is_error and result.status == "not_started"
    assert "no longer" in str(result.data).lower()


@pytest.mark.parametrize(
    "action", ["download", "remove", "update_information", "add_repository"]
)
async def test_marketplace_operations_delegate_and_verify(marketplace, action):
    arguments = (
        {"repository": "example/new", "category": "integration"}
        if action == "add_repository"
        else {"repository_id": "1"}
    )
    result = await call(marketplace, action, **arguments)
    assert not result.is_error, result.data
    assert result.data["data"]["store"] == "marketplace"
    marketplace.owner.data.async_write.assert_awaited_once()
    if action == "download":
        marketplace.repo.async_install_repository.assert_awaited_once_with(ref=None)
        marketplace.repo.async_download_repository.assert_not_awaited()


@pytest.mark.parametrize("race", ["owner", "consent", "github", "repository", "actor"])
async def test_marketplace_dispatch_rechecks_live_admission(marketplace, race):
    async def visibility(*_args):
        if race == "owner":
            marketplace.entry.runtime_data = copy.copy(marketplace.owner)
        elif race == "consent":
            marketplace.owner.warning_accepted.return_value = False
        elif race == "github":
            marketplace.owner.githubapi = object()
        elif race == "repository":
            marketplace.repositories["1"] = copy.copy(marketplace.repo)
        else:
            marketplace.state.active = False

    marketplace.manager.visibility.before.side_effect = visibility
    result = await call(marketplace, "download", repository_id="1")
    assert result.is_error and result.status == "not_started", result.data
    marketplace.repo.async_install_repository.assert_not_awaited()
    marketplace.owner.data.async_write.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [RuntimeError("download_failed"), asyncio.CancelledError()]
)
async def test_marketplace_unknown_outcome_does_not_replay(marketplace, error):
    def fail():
        raise error

    marketplace.repo.after = fail
    result = await call(marketplace, "download", repository_id="1")
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert result.data["error"]["code"] == "OUTCOME_UNKNOWN"
    marketplace.repo.async_install_repository.assert_awaited_once()
    marketplace.repo.async_download_repository.assert_not_awaited()


async def test_marketplace_registration_error_list_is_not_success(marketplace):
    marketplace.owner.async_register_repository.side_effect = None
    marketplace.owner.async_register_repository.return_value = [
        RuntimeError("invalid manifest")
    ]
    result = await call(
        marketplace, "add_repository", repository="example/new", category="integration"
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert "invalid manifest" in str(result.data)
    marketplace.owner.async_register_repository.assert_awaited_once()


async def test_marketplace_add_requires_github_before_any_owner_mutation(marketplace):
    marketplace.owner.github_connected = False
    result = await call(
        marketplace, "add_repository", repository="example/new", category="integration"
    )
    assert result.is_error and result.status == "not_started"
    assert "github_not_connected" in str(result.data)
    marketplace.owner.async_register_repository.assert_not_awaited()


async def test_marketplace_uninstall_preserves_live_config_entries(marketplace):
    marketplace.hass.config_entries.async_entries = lambda domain: [object()]
    result = await call(marketplace, "remove", repository_id="1")
    assert result.is_error and result.status == "not_started"
    marketplace.repo.uninstall.assert_not_awaited()


async def test_marketplace_first_install_adds_update_entity_signal(marketplace):
    marketplace.repo.data.installed = False
    result = await call(marketplace, "download", repository_id="1")
    assert not result.is_error, result.data
    marketplace.signal.assert_called_once_with(
        marketplace.hass, "installed", marketplace.repo
    )


async def test_marketplace_self_install_reports_files_not_active_runtime(marketplace):
    marketplace.repo.data.domain = "ha_mcp_tools"
    result = await call(marketplace, "download", repository_id="1")
    assert not result.is_error, result.data
    row = result.data["data"]
    assert row["runtime_still_loaded"] and row["pending_restart"]
    assert not row["active_version_verified"] and not row["restart_requested"]


@pytest.fixture
def update_entity(marketplace, monkeypatch):
    import importlib

    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.hacs_updates"
    )
    marketplace.hass.config.components.add("update")
    entity = SimpleNamespace(
        repository=marketplace.repo,
        marketplace=marketplace.owner,
        entity_id="update.renamed_by_user",
        unique_id="1",
    )
    target = SimpleNamespace(entity_id=entity.entity_id, entity=entity, unique_id="1")
    row = SimpleNamespace(
        platform="marketplace",
        unique_id="1",
        config_entry_id=marketplace.entry.entry_id,
    )
    registry = SimpleNamespace(
        async_get_entity_id=lambda *args: entity.entity_id, async_get=lambda key: row
    )
    registry_module = SimpleNamespace(async_get=lambda hass: registry)
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.entity_registry", registry_module
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "entity_registry",
        registry_module,
        raising=False,
    )
    monkeypatch.setattr(module, "update_target", lambda hass, entity_id: target)
    monkeypatch.setattr(module, "check_update_target", Mock())
    service = SimpleNamespace(schema=Mock(side_effect=lambda data: data))

    async def install(domain, action, data, **kwargs):
        await marketplace.repo._download(ref=data.get("version"))

    marketplace.hass.services = SimpleNamespace(
        async_services_for_domain=lambda domain: {"install": service},
        async_call=AsyncMock(side_effect=install),
    )
    monkeypatch.setattr(module, "Context", SimpleNamespace)
    return SimpleNamespace(entity=entity, row=row, service=service)


async def test_renamed_update_entity_uses_standard_service_with_actor(
    marketplace, update_entity
):
    marketplace.actor = CallContext("human")
    result = await call(marketplace, "download", repository_id="1", version="2")
    assert not result.is_error, result.data
    args = marketplace.hass.services.async_call.await_args
    assert args.args == (
        "update",
        "install",
        {"entity_id": "update.renamed_by_user", "version": "2"},
    )
    assert args.kwargs["context"].user_id == "human" and args.kwargs["blocking"]
    marketplace.repo.async_install_repository.assert_not_awaited()


async def test_wrong_update_entity_owner_refuses_without_private_fallback(
    marketplace, update_entity
):
    update_entity.entity.repository = copy.copy(marketplace.repo)
    result = await call(marketplace, "download", repository_id="1")
    assert result.is_error and result.status == "not_started"
    marketplace.hass.services.async_call.assert_not_awaited()
    marketplace.repo.async_install_repository.assert_not_awaited()


async def test_update_service_failure_is_unknown_without_direct_retry(
    marketplace, update_entity
):
    marketplace.hass.services.async_call.side_effect = RuntimeError(
        "service response lost"
    )
    result = await call(marketplace, "download", repository_id="1")
    assert result.is_error and result.status == "outcome_unknown"
    marketplace.hass.services.async_call.assert_awaited_once()
    marketplace.repo.async_install_repository.assert_not_awaited()


async def test_standard_update_does_not_require_private_install_validator(
    marketplace, update_entity
):
    del marketplace.hass.data["websocket_api"]["marketplace/repository/install"]
    del marketplace.repo.async_install_repository
    result = await call(marketplace, "download", repository_id="1", version="2")
    assert not result.is_error, result.data
    marketplace.hass.services.async_call.assert_awaited_once()


async def test_unqualified_marketplace_owner_has_explicit_capability_error(marketplace):
    del marketplace.owner.system.removed
    result = await call(marketplace, "info", repository_id="1")
    assert result.is_error and result.data["error"]["code"] == "UNSUPPORTED_OPERATION"
    marketplace.repo.update_repository.assert_not_awaited()


async def test_self_repository_provenance_cannot_change_after_dispatch(marketplace):
    marketplace.repo.data.domain = "ha_mcp_tools"
    marketplace.repo.after = lambda: setattr(
        marketplace.repo.data, "full_name", "other/fork"
    )
    result = await call(marketplace, "download", repository_id="1")
    assert result.is_error and result.status == "outcome_unknown", result.data
    marketplace.repo.async_install_repository.assert_awaited_once()


async def test_catalog_rename_requires_explicit_repository_choice(marketplace):
    marketplace.owner.common.renamed_repositories["example/original"] = "other/fork"
    result = await call(
        marketplace,
        "add_repository",
        repository="example/original",
        category="integration",
    )
    assert result.is_error and result.status == "not_started"
    marketplace.owner.async_register_repository.assert_not_awaited()
