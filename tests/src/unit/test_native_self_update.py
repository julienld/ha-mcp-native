"""Self-update owner pins, pre-write evidence, restart and no-replay contracts."""

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from zipfile import ZipFile

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_updates import Entity
from .test_native_updates import native as native
from .test_native_updates import updates as updates
from .test_self_update import releases


@pytest.fixture
def self_update(updates: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_self_update"
    )
    storage = importlib.import_module(
        "custom_components.ha_mcp_tools.native_self_update_storage"
    )
    access_module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.hacs_access"
    )
    update_owner = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.hacs_updates"
    )
    monkeypatch.setattr(update_owner, "Context", SimpleNamespace)
    source = tmp_path / "ha_mcp_tools"
    source.mkdir()
    (source / "manifest.json").write_text('{"version":"7.0.0"}', encoding="utf-8")
    (source / "__init__.py").write_text("old code", encoding="utf-8")
    monkeypatch.setattr(module, "component_source", lambda expected: source)
    updates.entities.clear()
    entity = Entity("update.renamed_by_user")
    entity.unique_id = "123"
    entity.installed_version, entity.latest_version = "7.0.0", "7.1.0"
    owner = SimpleNamespace(version="2.0.5")
    repo = SimpleNamespace(
        data=SimpleNamespace(
            id="123",
            domain="ha_mcp_tools",
            full_name="homeassistant-ai/ha-mcp",
            category="integration",
            installed=True,
        ),
        releases=SimpleNamespace(
            objects=releases(("7.1.0", "Fix bugs"), ("7.0.0", "old"))
        ),
        pending_restart=False,
        content=SimpleNamespace(path=SimpleNamespace(local=str(source))),
    )
    owner.repositories = SimpleNamespace(
        get_by_id=lambda key: repo if key == "123" else None,
        list_all=[repo],
    )
    entity.repository, entity.hacs = repo, owner
    repo.hacs = owner
    updates.entities[entity.entity_id] = entity
    monkeypatch.setitem(
        sys.modules,
        "custom_components.hacs.update",
        SimpleNamespace(HacsRepositoryUpdateEntity=Entity),
    )
    entry = SimpleNamespace(
        entry_id="hacs-entry",
        domain="hacs",
        state=access_module.ConfigEntryState.LOADED,
        data={},
        options={},
    )
    owner.configuration = SimpleNamespace(config_entry=entry)
    owner.status = SimpleNamespace(startup=False)
    owner.system = SimpleNamespace(running=True, disabled=False)
    owner.data = object()
    owner.github, owner.githubapi, owner.common = object(), object(), object()
    row = SimpleNamespace(
        platform="hacs", config_entry_id=entry.entry_id, unique_id=entity.unique_id
    )
    registry = SimpleNamespace(
        async_get=lambda key: row if key in updates.entities else None,
        async_get_entity_id=lambda domain, platform, unique_id: (
            entity.entity_id if unique_id == "123" else None
        ),
    )
    er = SimpleNamespace(async_get=lambda hass: registry)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "entity_registry", er)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", er)
    hass = updates.manager.hass
    hass.config.components.add("hacs")
    hass.config.components.add("update")
    hass.config.path = lambda *parts: str(tmp_path.joinpath(*parts))
    hass.config_entries.async_get_entry = lambda key: (
        entry if key == entry.entry_id else None
    )
    hass.async_add_executor_job = asyncio.to_thread
    hass.data["hacs"] = owner
    updates.manager.entry = SimpleNamespace(
        data={"secret": "not-for-output"}, options={"runtime_mode": "native"}
    )
    service = module.NativeSelfUpdate(updates.manager)
    service.running_component_version = "7.0.0"

    async def invoke(arguments: dict[str, Any], actor: CallContext) -> Any:
        return await service.manage(arguments.get("action", "check"), actor)

    definitions = importlib.import_module(
        "custom_components.ha_mcp_tools.native_self_update_tools"
    )
    from dataclasses import replace

    definition = replace(
        definitions.self_update_definition(updates.manager), handler=invoke
    )
    engine = Engine(updates.manager.backend, [definition])
    return SimpleNamespace(
        **{key: value for key, value in vars(updates).items() if key != "service"},
        module=module,
        storage=storage,
        service=service,
        self_engine=engine,
        entity=entity,
        repo=repo,
        owner=owner,
        row=row,
        source=source,
        tmp_path=tmp_path,
    )


async def call(world: Any, action: str = "check", *, read_only: bool = False) -> Any:
    return await world.self_engine.call(
        "ha_manage_ha_mcp_updates",
        {"action": action},
        CallContext("actor", read_only=read_only),
    )


async def test_renamed_entity_is_found_by_owner_and_checks_write_nothing(
    self_update: Any,
) -> None:
    result = await call(self_update)
    assert result.data["entity_id"] == self_update.entity.entity_id
    assert result.data["impact"]["classification"] == "compatible"
    assert not self_update.calls
    assert not self_update.service.directory.exists()
    assert "not-for-output" not in json.dumps(result.data)


@pytest.mark.parametrize("defect", ["domain", "repository", "registry", "owner"])
async def test_friendly_name_cannot_substitute_for_installation_provenance(
    self_update: Any, defect: str
) -> None:
    if defect == "domain":
        self_update.repo.data.domain = "unrelated"
    elif defect == "repository":
        self_update.repo.data.full_name = "untrusted/ha-mcp"
    elif defect == "registry":
        self_update.row.unique_id = "other"
    else:
        self_update.manager.hass.data["hacs"] = object()
    result = await call(self_update)
    assert result.is_error or result.data["capability"] == "manual_required"
    assert not self_update.calls


async def test_read_only_actor_can_check_but_cannot_snapshot_or_install(
    self_update: Any,
) -> None:
    assert not (await call(self_update, read_only=True)).is_error
    with pytest.raises(ToolFailure, match="Writes are disabled"):
        await call(self_update, "update", read_only=True)
    assert not self_update.calls and not self_update.service.directory.exists()


@pytest.mark.parametrize("enforced_at", ["start", "source", "capture"])
async def test_visibility_enforcement_stops_self_update_before_next_write(
    self_update: Any, monkeypatch: pytest.MonkeyPatch, enforced_at: str
) -> None:
    from custom_components.ha_mcp_tools import native_visibility
    from custom_components.ha_mcp_tools.native_operations import update_reads

    world = self_update
    world.manager.settings_ui = SimpleNamespace(directory=world.tmp_path)
    world.manager.visibility = native_visibility.NativeVisibility(world.manager)
    monkeypatch.setattr(
        update_reads, "hidden_from_core", lambda *_args: {"sensor.secret"}
    )
    monkeypatch.setattr(
        native_visibility, "hidden_from_core", lambda *_args: {"sensor.secret"}
    )

    def enforce() -> None:
        (world.tmp_path / "entity_visibility.json").write_text(
            json.dumps(
                {"enabled": True, "enforce": True, "deny_entity_ids": ["sensor.secret"]}
            ),
            encoding="utf-8",
        )

    original_source = world.module.component_source
    original_capture = world.module.capture_code

    def source(*args: Any) -> Any:
        result = original_source(*args)
        if enforced_at == "source":
            enforce()
        return result

    def capture(*args: Any) -> Any:
        result = original_capture(*args)
        if enforced_at == "capture":
            enforce()
        return result

    snapshot = Mock(side_effect=capture)
    monkeypatch.setattr(world.module, "component_source", source)
    monkeypatch.setattr(world.module, "capture_code", snapshot)
    if enforced_at == "start":
        enforce()
        for action in ("check", "guide"):
            assert not (await call(world, action)).is_error
    result = await call(world, "update")
    assert result.is_error and result.status == "not_started", result
    assert result.data["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"
    assert not world.calls
    if enforced_at != "capture":
        snapshot.assert_not_called()
        assert not world.service.directory.exists()
    else:
        snapshot.assert_called_once()


@pytest.mark.parametrize("notes", ["BREAKING CHANGES: new authentication", ""])
async def test_breaking_or_unreviewed_release_is_rejected_before_files_change(
    self_update: Any, notes: str
) -> None:
    self_update.repo.releases.objects[0].body = notes
    result = await call(self_update, "update")
    assert result.is_error and result.status == "not_started"
    assert not self_update.calls and not self_update.service.directory.exists()


async def test_normal_update_captures_old_files_uses_actor_and_preserves_runtime(
    self_update: Any,
) -> None:
    result = await call(self_update, "update")
    assert not result.is_error, result.data
    assert result.data["status"] == "pending_restart"
    assert result.data["running_version"] == "7.0.0"
    assert result.data["installed_version"] == "7.1.0"
    assert self_update.manager.entry.options == {"runtime_mode": "native"}
    domain, action, data, kwargs = self_update.calls[0]
    assert (domain, action) == ("update", "install")
    assert data["version"] == "7.1.0"
    assert kwargs["context"].user_id == "actor"
    snapshot = self_update.service.directory / (
        result.data["recovery"]["snapshot_id"] + ".zip"
    )
    with ZipFile(snapshot) as archive:
        assert archive.read("__init__.py") == b"old code"
    assert result.data["restart"]["kind"] == "expected_interruption"


async def test_unknown_dispatch_is_not_replayed_even_in_a_new_service_instance(
    self_update: Any,
) -> None:
    self_update.manager.hass.services.async_call = AsyncMock(
        side_effect=TimeoutError("secret URL not-for-output")
    )
    first = await call(self_update, "update")
    assert first.status == "outcome_unknown"
    service = self_update.module.NativeSelfUpdate(self_update.manager)
    with pytest.raises(ToolFailure) as failure:
        await service.manage("update", CallContext("actor"))
    assert failure.value.status == "outcome_unknown"
    self_update.manager.hass.services.async_call.assert_awaited_once()
    assert "not-for-output" not in json.dumps(first.data)


async def test_success_response_without_installed_version_readback_is_unknown(
    self_update: Any,
) -> None:
    self_update.manager.hass.services.async_call = AsyncMock()
    result = await call(self_update, "update")
    assert result.status == "outcome_unknown"


async def test_owner_change_during_snapshot_prevents_dispatch(
    self_update: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = self_update.module.capture_code

    def changed(*args: Any) -> Any:
        receipt = original(*args)
        self_update.entity.latest_version = "8.0.0"
        return receipt

    monkeypatch.setattr(self_update.module, "capture_code", changed)
    result = await call(self_update, "update")
    assert result.is_error and result.status == "not_started"
    assert not self_update.calls


async def test_concurrent_calls_do_not_repeat_an_uncertain_dispatch(
    self_update: Any,
) -> None:
    self_update.manager.hass.services.async_call = AsyncMock(side_effect=TimeoutError())
    results = await asyncio.gather(
        call(self_update, "update"), call(self_update, "update")
    )
    assert all(result.status == "outcome_unknown" for result in results)
    self_update.manager.hass.services.async_call.assert_awaited_once()


async def test_readback_after_lost_reply_observes_install_without_replaying(
    self_update: Any,
) -> None:
    async def installed(*args: Any, **kwargs: Any) -> None:
        self_update.entity.installed_version = "7.1.0"
        raise TimeoutError()

    self_update.manager.hass.services.async_call = AsyncMock(side_effect=installed)
    assert (await call(self_update, "update")).status == "outcome_unknown"
    result = await call(self_update, "update")
    assert not result.is_error and result.data["status"] == "pending_restart"
    self_update.manager.hass.services.async_call.assert_awaited_once()


async def test_policy_revoked_during_snapshot_blocks_install(
    self_update: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = self_update.module.capture_code

    def revoked(*args: Any) -> Any:
        receipt = original(*args)
        self_update.self_engine.read_only = True
        return receipt

    monkeypatch.setattr(self_update.module, "capture_code", revoked)
    result = await call(self_update, "update")
    assert result.is_error and not self_update.calls


async def test_marketplace_uses_existing_actor_consent_before_snapshot_and_core_install(
    self_update: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = self_update
    hass = world.manager.hass
    entry = world.owner.configuration.config_entry
    hass.config.components.remove("hacs")
    hass.config.components.add("marketplace")
    entry.domain, entry.runtime_data = "marketplace", world.owner
    world.row.platform = "marketplace"
    world.owner.version = "2026.11.0.dev0"
    world.owner.system.removed = False
    world.owner.warning_accepted = Mock(return_value=False)
    world.repo.marketplace = world.owner
    world.entity.marketplace = world.owner
    hass.config_entries.async_loaded_entries = lambda domain: [entry]
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.marketplace.base",
        SimpleNamespace(async_get_marketplace=lambda hass: world.owner),
    )
    access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.marketplace_access"
    )
    entry.state = access.ConfigEntryState.LOADED
    result = await call(world, "update")
    assert result.is_error and result.data["error"]["code"] == "AUTH_FORBIDDEN"
    assert not world.calls and not world.service.directory.exists()
    world.owner.warning_accepted.return_value = True
    result = await call(world, "update")
    assert not result.is_error and result.data["status"] == "pending_restart", (
        result.data
    )
    assert world.calls[0][3]["context"].user_id == "actor"
    world.owner.warning_accepted.assert_called_with("actor")


async def test_verified_fork_updates_its_own_entity_without_switching_repository(
    self_update: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_self_update_owner"
    )
    self_update.repo.data.full_name = "example/my-fork"
    proof = Mock(return_value=True)
    monkeypatch.setattr(owner_module, "paired_fork_origin", proof)
    result = await call(self_update, "update")
    assert not result.is_error, result.data
    assert result.data["repository"] == "example/my-fork"
    assert self_update.repo.data.full_name == "example/my-fork"
    assert self_update.calls[0][2]["entity_id"] == self_update.entity.entity_id
    proof.assert_called_once_with("example/my-fork", "7.0.0")


async def test_repository_switch_during_preparation_is_never_dispatched(
    self_update: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = self_update.module.capture_code

    def switched(*args: Any) -> Any:
        receipt = original(*args)
        self_update.repo.data.full_name = "homeassistant-ai/ha-mcp-integration"
        return receipt

    monkeypatch.setattr(self_update.module, "capture_code", switched)
    result = await call(self_update, "update")
    assert result.is_error and not self_update.calls


async def test_guide_remains_available_while_store_is_starting(
    self_update: Any,
) -> None:
    self_update.owner.status.startup = True
    result = await call(self_update, "guide")
    assert not result.is_error
    assert result.data["owner_discovery_error"] == "SERVICE_UNAVAILABLE"
    assert result.data["guide"]["steps"]
    assert not self_update.calls
