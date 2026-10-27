"""Update ownership, visibility, partial outcomes and self-affecting dispatch."""

import asyncio
import copy
import importlib
import sys
from enum import IntFlag
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native


class Features(IntFlag):
    INSTALL = 1
    RELEASE_NOTES = 16


class Entity:
    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self.unique_id = entity_id + "-unique"
        self.latest_version = "2.0"
        self.installed_version = "1.0"
        self.in_progress = False
        self.available = True
        self.supported_features = Features.INSTALL | Features.RELEASE_NOTES
        self.skipped = None
        self.async_release_notes = AsyncMock(return_value="Original provider notes")

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "state": "on",
            "attributes": {
                "title": "Native firmware",
                "latest_version": self.latest_version,
                "installed_version": self.installed_version,
                "in_progress": self.in_progress,
                "skipped_version": self.skipped,
                "supported_features": self.supported_features.value,
                "future_provider_field": {"format": "new"},
            },
        }


class CoreUpdate(Entity):
    pass


@pytest.fixture
def updates(native: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.update",
        SimpleNamespace(
            DATA_COMPONENT="update", UpdateEntity=Entity, UpdateEntityFeature=Features
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.hassio.update",
        SimpleNamespace(
            SupervisorCoreUpdateEntity=CoreUpdate,
            SupervisorOSUpdateEntity=type("OSUpdate", (Entity,), {}),
            SupervisorSupervisorUpdateEntity=type("SupervisorUpdate", (Entity,), {}),
        ),
    )
    entities = {name: Entity(name) for name in ("update.native_a", "update.native_b")}
    calls: list[Any] = []
    service = SimpleNamespace(schema=lambda data: data)

    async def call(
        domain: str, action: str, data: dict[str, Any], **kwargs: Any
    ) -> None:
        calls.append((domain, action, copy.deepcopy(data), kwargs))
        entity = entities[data["entity_id"]]
        if action == "skip":
            entity.skipped = entity.latest_version
        elif action == "clear_skipped":
            entity.skipped = None
        else:
            entity.installed_version = entity.latest_version

    hass = SimpleNamespace(
        data={"update": SimpleNamespace(get_entity=entities.get)},
        config=SimpleNamespace(components=set()),
        config_entries=SimpleNamespace(async_entries=list),
        states=SimpleNamespace(
            get=entities.get, async_all=lambda domain: list(entities.values())
        ),
        services=SimpleNamespace(
            async_services_for_domain=lambda domain: dict.fromkeys(
                ("install", "skip", "clear_skipped"), service
            ),
            async_call=AsyncMock(side_effect=call),
        ),
        async_create_task=lambda coro, name: asyncio.create_task(coro, name=name),
    )
    manager = SimpleNamespace(
        hass=hass,
        backend=SimpleNamespace(authorize=AsyncMock()),
        visibility=None,
        accepting=True,
        policy=SimpleNamespace(security_policy=None),
        refresh_policy=AsyncMock(),
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_update_tools"
    )
    writes = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.update_writes"
    )
    monkeypatch.setattr(writes, "Context", SimpleNamespace)
    monkeypatch.setattr(sys.modules["homeassistant.core"], "Context", SimpleNamespace)
    lifecycle = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.system_lifecycle"
    )
    manager.system_lifecycle = lifecycle.NativeSystemLifecycle(manager, reply_delay=60)
    engine = Engine(manager.backend, [module.update_definition(manager)])
    return SimpleNamespace(
        manager=manager, engine=engine, entities=entities, calls=calls, service=service
    )


async def invoke(updates: Any, **arguments: Any) -> Any:
    return await updates.engine.call(
        "ha_manage_updates", arguments, CallContext("actor")
    )


async def test_list_and_original_release_notes_use_core_owners(updates: Any) -> None:
    listed = await invoke(updates)
    assert listed.data["updates_available"] == 2 and not listed.is_error
    assert listed.data["updates"][0]["attributes"]["future_provider_field"] == {
        "format": "new"
    }
    result = await invoke(updates, action="get", entity_ids="update.native_a")
    assert result.data["release_notes"] == "Original provider notes", result.data
    assert result.data["release_notes_source"] == "core"
    updates.entities["update.native_a"].async_release_notes.assert_awaited_once()
    assert not updates.calls


async def test_successful_service_return_does_not_falsely_confirm_skip(
    updates: Any,
) -> None:
    updates.manager.hass.services.async_call.side_effect = None
    result = await invoke(updates, action="skip", entity_ids="update.native_a")
    assert result.status == "outcome_unknown" and not result.data["success"], (
        result.data
    )
    updates.manager.hass.services.async_call.assert_awaited_once()


@pytest.fixture
def repairs(updates: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    rows = {
        ("fixture", "a"): SimpleNamespace(active=True, dismissed_version=None),
        ("fixture", "b"): SimpleNamespace(active=False, dismissed_version="old"),
    }
    calls = []

    def ignore(domain: str, issue_id: str, value: bool) -> Any:
        calls.append((domain, issue_id, value))
        row = copy.copy(rows[(domain, issue_id)])
        row.dismissed_version = "core" if value else None
        rows[(domain, issue_id)] = row
        return row

    owner = SimpleNamespace(
        async_get_issue=lambda domain, identifier: rows.get((domain, identifier)),
        async_ignore=ignore,
    )
    fake = SimpleNamespace(async_get=lambda hass: owner)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", fake)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "issue_registry", fake, raising=False
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.update_writes"
    )
    validators = []

    def validate(hass: Any, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        validators.append((command, payload))
        return payload

    monkeypatch.setattr(module, "_validated_payload", validate)
    return SimpleNamespace(
        world=updates, rows=rows, owner=owner, calls=calls, validators=validators
    )


async def test_repairs_use_core_validator_and_actual_registry_including_inactive_unignore(
    repairs: Any,
) -> None:
    result = await invoke(
        repairs.world,
        action="ignore_repair",
        repairs=[{"domain": "fixture", "issue_id": "a", "irrelevant": True}],
    )
    assert result.data["succeeded"] == 1, result.data
    assert repairs.rows[("fixture", "a")].dismissed_version == "core"
    assert repairs.validators == [
        ("repairs/ignore_issue", {"domain": "fixture", "issue_id": "a", "ignore": True})
    ]
    result = await invoke(
        repairs.world,
        action="unignore_repair",
        repairs=[{"domain": "fixture", "issue_id": "b"}],
    )
    assert result.data["succeeded"] == 1, result.data
    assert repairs.rows[("fixture", "b")].dismissed_version is None


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_repair_after_effect_failure_is_unknown_without_replay(
    repairs: Any, failure: type[BaseException]
) -> None:
    original = repairs.owner.async_ignore

    def fail(*args: Any) -> Any:
        original(*args)
        raise failure("after save")

    repairs.owner.async_ignore = fail
    result = await invoke(
        repairs.world,
        action="ignore_repair",
        repairs=[{"domain": "fixture", "issue_id": "a"}] * 2,
    )
    assert result.status == "outcome_unknown", result.data
    assert repairs.calls == [("fixture", "a", True)]
    assert result.data["results"][1]["not_attempted"]


async def test_update_provider_replaced_while_reading_is_not_returned(
    updates: Any,
) -> None:
    async def notes() -> str:
        updates.entities["update.native_a"] = Entity("update.native_a")
        return "stale notes"

    updates.entities["update.native_a"].async_release_notes.side_effect = notes
    result = await invoke(updates, action="get", entity_ids="update.native_a")
    assert result.is_error and "stale notes" not in str(result.data), result.data


async def test_core_service_has_real_actor_and_returned_state(updates: Any) -> None:
    result = await invoke(updates, action="skip", entity_ids=["update.native_a"])
    assert result.data["succeeded"] == 1 and not result.is_error, result.data
    assert updates.entities["update.native_a"].skipped == "2.0"
    assert (
        updates.calls[0][3]["blocking"]
        and updates.calls[0][3]["context"].user_id == "actor"
    )


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_after_effect_failure_is_unknown_and_stops_remaining_targets(
    updates: Any, failure: type[BaseException]
) -> None:
    async def fail(
        domain: str, action: str, data: dict[str, Any], **kwargs: Any
    ) -> None:
        updates.calls.append(data["entity_id"])
        updates.entities[data["entity_id"]].installed_version = "2.0"
        raise failure("after effect")

    updates.manager.hass.services.async_call.side_effect = fail
    result = await invoke(updates, action="install", entity_ids=list(updates.entities))
    assert result.status == "outcome_unknown" and result.data["failed"] == 2, (
        result.data
    )
    assert updates.calls == ["update.native_a"]
    assert result.data["results"][1]["not_attempted"]


async def test_read_only_calls_keep_reads_but_never_dispatch(updates: Any) -> None:
    actor = CallContext("actor", read_only=True)
    listed = await updates.engine.call("ha_manage_updates", {}, actor)
    with pytest.raises(ToolFailure) as refused:
        await updates.engine.call(
            "ha_manage_updates",
            {"action": "install", "entity_ids": "update.native_a"},
            actor,
        )
    assert not listed.is_error and refused.value.code == "READ_ONLY"
    assert not updates.calls


async def test_renamed_core_update_is_scheduled_and_never_part_of_install_all(
    updates: Any,
) -> None:
    updates.manager.hass.config.components.add("hassio")
    updates.entities["update.native_a"] = CoreUpdate("update.native_a")
    applied = await invoke(updates, action="install", categories=["devices"])
    assert applied.data["success"], applied.data
    assert [row[2]["entity_id"] for row in updates.calls] == ["update.native_b"]
    updates.calls.clear()
    scheduled = await invoke(
        updates, action="install", entity_ids=list(updates.entities)
    )
    assert scheduled.data["results"][0]["scheduled"], scheduled.data
    assert scheduled.data["results"][1]["not_attempted"]
    assert not updates.calls
    await updates.manager.system_lifecycle.stop()


async def test_scheduled_update_rechecks_identity_before_core_handoff(
    updates: Any,
) -> None:
    updates.manager.hass.config.components.add("hassio")
    updates.entities["update.native_a"] = CoreUpdate("update.native_a")
    lifecycle = updates.manager.system_lifecycle
    lifecycle.reply_delay = 0
    result = await invoke(updates, action="install", entity_ids="update.native_a")
    assert result.data["results"][0]["scheduled"], result.data
    updates.entities["update.native_a"] = CoreUpdate("update.native_a")
    await asyncio.gather(*tuple(lifecycle.pending))
    assert not updates.calls


async def test_scheduled_update_handoff_is_core_owned_and_keeps_actor(
    updates: Any,
) -> None:
    from ha_mcp.runtime.mutation_guard import current_admission

    updates.manager.hass.config.components.add("hassio")
    updates.entities["update.native_a"] = CoreUpdate("update.native_a")
    lifecycle = updates.manager.system_lifecycle
    lifecycle.reply_delay = 0
    owned = []
    observations = []

    def create_task(coro: Any, name: str) -> Any:
        task = asyncio.create_task(coro, name=name)
        owned.append(task)
        return task

    async def call(*args: Any, **kwargs: Any) -> None:
        observations.append(
            (args, kwargs, current_admission.get(), bool(lifecycle.pending))
        )

    updates.manager.hass.async_create_task = create_task
    updates.manager.hass.services.async_call.side_effect = call
    result = await invoke(
        updates, action="install", entity_ids="update.native_a", backup=True
    )
    assert result.data["results"][0]["scheduled"], result.data
    await asyncio.gather(*tuple(lifecycle.pending))
    await asyncio.gather(*owned)
    args, kwargs, admission, pending = observations[0]
    assert args == (
        "update",
        "install",
        {"entity_id": "update.native_a", "backup": True},
    )
    assert kwargs["blocking"] is False and kwargs["context"].user_id == "actor"
    assert admission is None and not pending


async def test_update_rechecks_role_after_service_validation(updates: Any) -> None:
    def validate(data: dict[str, Any]) -> dict[str, Any]:
        updates.manager.backend.authorize.side_effect = PermissionError("revoked")
        return data

    updates.service.schema = validate
    result = await invoke(updates, action="skip", entity_ids="update.native_a")
    assert result.is_error and result.status == "not_started", result.data
    assert not updates.calls


async def test_update_owner_drains_two_cancellations_before_releasing_its_lock(
    updates: Any,
) -> None:
    from ha_mcp.utils.registry_update_lock import registry_update_lock

    entered, release = asyncio.Event(), asyncio.Event()

    async def call(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await release.wait()
        entity = updates.entities[args[2]["entity_id"]]
        entity.skipped = entity.latest_version

    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.updates"
    )
    service = module.NativeUpdates(updates.manager, CallContext("actor"))
    updates.manager.hass.services.async_call.side_effect = call
    task = asyncio.create_task(
        service.manage(action="skip", entity_ids="update.native_a")
    )
    await asyncio.wait_for(entered.wait(), 1)

    async def acquire() -> None:
        async with registry_update_lock("update", "update.native_a"):
            return

    waiter = asyncio.create_task(acquire())
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not waiter.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    await asyncio.wait_for(waiter, 1)
