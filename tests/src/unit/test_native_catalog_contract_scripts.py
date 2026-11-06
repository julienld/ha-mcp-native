"""Script authoring shares complete behavior over actual native owner seams."""

import asyncio
import copy
import importlib
import sys
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.script_writes import ScriptWriteTools
from ha_mcp.tool_api import CallContext
from ha_mcp.utils.config_hash import compute_config_hash

from ._component_ws_fakes import _FakeCallServices
from .test_component_ws_search import (
    FakeEntityReg,
    FakeRegEntry,
    FakeState,
    make_view,
    wsapi,
)
from .test_native_blueprint_render import (
    _BlueprintFailure,
    _DomainOwner,
    _UndefinedInput,
)
from .test_native_catalog_contract_config import (
    _real_aiohttp_router as _real_aiohttp_router,
)
from .test_native_config_mutations import _mutation_setup
from .test_native_engine_backend import EventBus
from .test_native_engine_backend import native as native


class _ScriptRegistry(FakeEntityReg):
    def async_get_entity_id(
        self, domain: str, platform: str, unique_id: str
    ) -> str | None:
        return next(
            (
                entry.entity_id
                for entry in self.entities.values()
                if entry.platform == platform
                and entry.unique_id == unique_id
                and entry.entity_id.startswith(f"{domain}.")
            ),
            None,
        )


def _setup_script(
    native: Any, monkeypatch: pytest.MonkeyPatch, *, backup: Any = None
) -> tuple:
    owner, hass, _, writes, tasks, _ = _mutation_setup(monkeypatch, "script")
    owner.current = {
        "uid": {"alias": "Before", "sequence": [{"delay": 1}], "mode": "single"}
    }
    entry = FakeRegEntry("script.renamed", unique_id="uid", platform="script")
    registry = _ScriptRegistry({entry.entity_id: entry})
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "entity_registry",
        SimpleNamespace(async_get=lambda hass: registry),
    )
    monkeypatch.setattr(wsapi.er, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"].config_validation,
        "entity_id",
        lambda value: value,
    )
    hass.states._by_id[entry.entity_id] = FakeState(entry.entity_id)
    hass.bus = EventBus()
    waiters = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.entity_waiters"
    )
    monkeypatch.setattr(waiters, "callback", lambda function: function)
    hass.services = _FakeCallServices(
        known={("script", "turn_on"), ("script", "turn_off")}
    )
    hass.services.async_services = lambda: {"script": {"turn_on": {}, "turn_off": {}}}
    backend = native.NativeBackend(
        hass, config_backup=backup or AsyncMock(return_value={})
    )
    definition = define_tool(
        backend,
        ScriptWriteTools,
        "ha_config_set_script",
        annotations={"readOnlyHint": False},
    )
    return Engine(backend, [definition]), owner, hass, registry, writes, tasks


async def test_native_script_replacement_preserves_canonical_target_and_snapshot(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = []

    async def backup(payload: dict, context: CallContext) -> dict:
        captured.append(copy.deepcopy(payload))
        return {}

    engine, owner, _, _, writes, tasks = _setup_script(
        native, monkeypatch, backup=backup
    )
    result = await engine.call(
        "ha_config_set_script",
        {
            "script_id": "script.renamed",
            "config": {"sequence": [{"delay": 2}]},
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert result.data["script_id"] == "uid" and len(writes) == 1
    assert owner.current == {"uid": {"alias": "renamed", "sequence": [{"delay": 2}]}}
    assert captured == [
        {
            "domain": "script",
            "source_tool": "ha_config_set_script",
            "backup_id": "uid",
            "current_config": {
                "alias": "Before",
                "sequence": [{"delay": 1}],
                "mode": "single",
            },
        }
    ]


async def test_native_script_transform_uses_original_hash_and_reports_fresh_hash(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, owner, _, _, writes, tasks = _setup_script(native, monkeypatch)
    before = compute_config_hash(owner.current["uid"])
    result = await engine.call(
        "ha_config_set_script",
        {
            "script_id": "renamed",
            "python_transform": "config['sequence'].append({'delay': 2})",
            "config_hash": before,
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert result.data["config_hash"] == compute_config_hash(owner.current["uid"])
    assert result.data["config_hash"] != before and len(writes) == 1
    assert owner.current["uid"]["sequence"] == [{"delay": 1}, {"delay": 2}]


async def test_native_script_take_control_renders_and_preserves_human_labels(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, owner, hass, _, writes, tasks = _setup_script(native, monkeypatch)
    owner.current = {
        "uid": {
            "alias": "Human",
            "description": "Keep",
            "use_blueprint": {
                "path": "author/example.yaml",
                "input": {"message": "Hello"},
            },
        }
    }
    renderer = _DomainOwner(hass)
    hass.data["blueprint"] = {"script": renderer}
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.blueprint.models",
        SimpleNamespace(DomainBlueprints=_DomainOwner),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.blueprint.errors",
        SimpleNamespace(BlueprintException=_BlueprintFailure),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.util.yaml",
        SimpleNamespace(UndefinedSubstitution=_UndefinedInput),
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"].config_validation,
        "path",
        lambda value: value,
        raising=False,
    )
    result = await engine.call(
        "ha_config_set_script",
        {
            "script_id": "renamed",
            "take_control_of_blueprint": True,
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert result.data["took_control_of_blueprint"] == "author/example.yaml"
    assert owner.current == {
        "uid": {
            "alias": "Human",
            "description": "Keep",
            "sequence": [{"delay": 1}],
            "mode": "queued",
        }
    }
    assert len(writes) == 1


@pytest.mark.parametrize("run", ["start", "stop"])
async def test_native_script_runtime_control_dispatches_once_without_config_snapshot(
    native: Any, monkeypatch: pytest.MonkeyPatch, run: str
) -> None:
    backup = AsyncMock(return_value={})
    engine, _, hass, _, writes, _ = _setup_script(native, monkeypatch, backup=backup)
    args = {"script_id": "renamed", "run": run, "MandatoryBPS": False}
    if run == "start":
        args["variables"] = {"message": "Hello"}
    result = await engine.call("ha_config_set_script", args, CallContext("actor"))
    assert not result.is_error, result.data
    assert result.data["action"] == run and result.data["entity_id"] == "script.renamed"
    assert len(hass.services.calls) == 1
    assert hass.services.calls[0]["service"] == (
        "turn_on" if run == "start" else "turn_off"
    )
    assert writes == []
    backup.assert_not_awaited()


async def test_native_script_refuses_changes_landing_after_hash_verification(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, owner, _, _, writes, tasks = _setup_script(native, monkeypatch)
    before = compute_config_hash(owner.current["uid"])
    definition = engine.tools["ha_config_set_script"]
    original = owner.data_validator

    async def validate(hass: Any, key: str, value: dict) -> Any:
        owner.current["uid"]["alias"] = "Concurrent edit"
        return await original(hass, key, value)

    owner.data_validator = validate
    result = await engine.call(
        definition.name,
        {
            "script_id": "renamed",
            "python_transform": "config['sequence'].append({'delay': 2})",
            "config_hash": before,
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert writes == tasks == [] and owner.current["uid"]["alias"] == "Concurrent edit"


@pytest.mark.parametrize("where", ["verify", "backup"])
async def test_script_cas_does_not_equate_boolean_and_number(
    native: Any, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    async def backup(payload: dict, context: CallContext) -> dict:
        if where == "backup":
            owner.current["uid"]["enabled"] = 0
        return {}

    engine, owner, _, _, writes, tasks = _setup_script(
        native, monkeypatch, backup=backup
    )
    owner.current["uid"]["enabled"] = False

    async def validate_categories(self: Any, categories: dict) -> None:
        if where == "verify":
            owner.current["uid"]["enabled"] = 0

    monkeypatch.setattr(ScriptWriteTools, "_validate_categories", validate_categories)
    result = await engine.call(
        "ha_config_set_script",
        {
            "script_id": "renamed",
            "python_transform": "config['mode'] = 'queued'",
            "config_hash": compute_config_hash(owner.current["uid"]),
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert writes == [] and type(owner.current["uid"]["enabled"]) is int


async def test_remote_script_create_and_transform_use_the_existing_client_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient

    monkeypatch.setattr(
        "ha_mcp.config.get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=False, enable_mandatory_bps=False),
    )
    client = HomeAssistantClient.__new__(HomeAssistantClient)
    client._resolve_script_id = AsyncMock(return_value="uid")
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": []}
    )
    stored = {}
    posts = []

    async def request(method: str, path: str, **arguments: Any) -> dict:
        assert path == "config/script/config/uid"
        if method == "GET":
            if not stored:
                raise HomeAssistantAPIError("Not found", status_code=404)
            return copy.deepcopy(stored)
        assert method == "POST"
        posts.append(copy.deepcopy(arguments["json"]))
        stored.clear()
        stored.update(arguments["json"])
        return {"result": "ok"}

    client._request = request
    backend = RemoteBackend(client)
    definition = define_tool(
        backend,
        ScriptWriteTools,
        "ha_config_set_script",
        annotations={"readOnlyHint": False},
    )
    engine = Engine(backend, [definition])
    created = await engine.call(
        "ha_config_set_script",
        {
            "script_id": "renamed",
            "config": {"sequence": [{"delay": 1}]},
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("remote"),
    )
    assert not created.is_error, created.data
    assert created.data["script_id"] == "uid" and stored["alias"] == "renamed"
    updated = await engine.call(
        "ha_config_set_script",
        {
            "script_id": "renamed",
            "python_transform": "config['sequence'].append({'delay': 2})",
            "config_hash": compute_config_hash(stored),
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("remote"),
    )
    assert not updated.is_error, updated.data
    assert updated.data["config_hash"] == compute_config_hash(stored)
    assert len(posts) == 2 and stored["sequence"] == [{"delay": 1}, {"delay": 2}]


async def test_native_script_wait_resolves_the_entity_registered_by_core_hook(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, owner, hass, registry, writes, tasks = _setup_script(native, monkeypatch)
    owner.current = {}
    registry.entities.clear()
    registry._entries.clear()

    async def register(action: str, key: str) -> None:
        entry = FakeRegEntry("script.name_from_core", platform="script", unique_id=key)
        registry.entities[entry.entity_id] = entry
        registry._entries[entry.entity_id] = entry
        hass.states._by_id[entry.entity_id] = FakeState(entry.entity_id)

    owner.post_write_hook = register
    result = await engine.call(
        "ha_config_set_script",
        {
            "script_id": "new",
            "config": {"alias": "Name from Core", "sequence": [{"delay": 1}]},
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert result.data["script_id"] == "new" and len(writes) == 1
    assert "warnings" not in result.data
    assert hass.bus.listeners == []


@pytest.mark.parametrize(
    "category_state", ["present", "removed_during_write", "missing"]
)
async def test_native_script_categories_validate_before_write_and_recheck_after(
    native: Any, monkeypatch: pytest.MonkeyPatch, category_state: str
) -> None:
    engine, owner, _, registry, writes, tasks = _setup_script(native, monkeypatch)
    present = category_state != "missing"
    row = SimpleNamespace(category_id="cat", name="Work", icon=None)

    def list_categories(*, scope: str) -> list:
        assert scope == "script"
        return [row] if present else []

    def get_category(*, scope: str, category_id: str) -> Any:
        assert scope == "script" and category_id == "cat"
        return row if present else None

    categories = SimpleNamespace(
        async_list_categories=list_categories, async_get_category=get_category
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "category_registry",
        SimpleNamespace(async_get=lambda hass: categories),
        raising=False,
    )
    monkeypatch.setattr(
        wsapi, "_resolve_registries", lambda hass: make_view(entity=registry.entities)
    )
    registry.async_update_entity = Mock()
    original = owner.data_validator

    async def validate(hass: Any, key: str, value: dict) -> Any:
        nonlocal present
        if category_state == "removed_during_write":
            present = False
        return await original(hass, key, value)

    owner.data_validator = validate
    result = await engine.call(
        "ha_config_set_script",
        {
            "script_id": "renamed",
            "config": {"sequence": [{"delay": 2}]},
            "category": "cat",
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    if category_state == "missing":
        assert result.is_error and writes == []
    elif category_state == "removed_during_write":
        assert not result.is_error and len(writes) == 1
        assert any("no longer exists" in message for message in result.data["warnings"])
    else:
        assert not result.is_error and result.data["category"] == "cat"
        registry.async_update_entity.assert_called_once_with(
            "script.renamed", categories={"script": "cat"}
        )
    if category_state != "present":
        registry.async_update_entity.assert_not_called()


async def test_native_script_reference_warnings_and_skill_content_survive_the_port(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, _, _, writes, tasks = _setup_script(native, monkeypatch)
    requested = []

    def read(references: list[str], *, enabled: bool) -> dict:
        requested.extend(references)
        return {
            "content": dict.fromkeys(references, "Guidance"),
            "enabled": enabled,
            "vendor_missing": False,
        }

    monkeypatch.setattr(native, "read_skill_files", read)
    result = await engine.call(
        "ha_config_set_script",
        {
            "script_id": "renamed",
            "config": {
                "sequence": [
                    {
                        "action": "notify.missing",
                        "target": {"entity_id": "light.missing"},
                    }
                ]
            },
            "wait": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert len(writes) == 1
    assert {warning["kind"] for warning in result.data["validation"]["warnings"]} == {
        "entity",
        "service",
    }
    assert set(requested) == {
        "references/automation-actions.md",
        "references/triggers-and-conditions.md",
        "references/template-guidelines.md",
    }
    assert next(iter(result.data)) == "skill_content_hint"


async def test_native_transform_keeps_home_assistant_loop_responsive(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, owner, hass, _, writes, tasks = _setup_script(native, monkeypatch)
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.config_finalize"
    )
    execute = module.safe_execute
    started, release = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def blocked(expression: str, config: dict) -> Any:
        started.set()
        assert release.wait(5), "Test did not release its transform worker"
        return execute(expression, config)

    monkeypatch.setattr(module, "safe_execute", blocked)
    hass.async_add_executor_job = lambda function, *args: loop.run_in_executor(
        None, function, *args
    )
    operation = asyncio.create_task(
        engine.call(
            "ha_config_set_script",
            {
                "script_id": "renamed",
                "python_transform": "config['sequence'].append({'delay': 2})",
                "config_hash": compute_config_hash(owner.current["uid"]),
                "wait": False,
                "MandatoryBPS": False,
            },
            CallContext("actor"),
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        pulse = asyncio.Event()
        loop.call_soon(pulse.set)
        await asyncio.wait_for(pulse.wait(), 0.2)
        assert writes == [] and not operation.done()
    finally:
        release.set()
    result = await operation
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert len(writes) == 1
