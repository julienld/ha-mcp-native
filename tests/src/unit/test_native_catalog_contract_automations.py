"""Automation authoring preserves identity, snapshots and runtime ordering."""

import asyncio
import copy
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime import Engine
from ha_mcp.runtime.automation_writes import AutomationWriteTools
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.config_normalize import _normalize_config_for_roundtrip
from ha_mcp.runtime.config_reload import config_reload_waiter
from ha_mcp.tool_api import CallContext
from ha_mcp.utils.config_hash import compute_config_hash

from ._component_ws_fakes import _FakeCallServices
from .test_component_ws_search import FakeRegEntry, FakeState
from .test_native_catalog_contract_config import (
    _real_aiohttp_router as _real_aiohttp_router,
)
from .test_native_catalog_contract_scripts import _ScriptRegistry
from .test_native_config_mutations import _mutation_setup
from .test_native_engine_backend import native as native
from .test_native_engine_reload import Bus
from .test_native_engine_reload import _core_callback as _core_callback


def _setup_automation(
    native: Any, monkeypatch: pytest.MonkeyPatch, *, backup: Any = None
) -> tuple:
    owner, hass, _, writes, tasks, _ = _mutation_setup(monkeypatch, "automation")
    owner.current = [
        {"id": "uid", "alias": "Before", "trigger": [], "action": [{"delay": 1}]}
    ]
    entry = FakeRegEntry("automation.renamed", unique_id="uid", platform="automation")
    registry = _ScriptRegistry({entry.entity_id: entry})
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "entity_registry",
        SimpleNamespace(async_get=lambda hass: registry),
    )
    hass.states._by_id[entry.entity_id] = FakeState(entry.entity_id, "on", id="uid")
    hass.states.async_all = lambda: list(hass.states._by_id.values())
    hass.bus = Bus()
    waiters = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.entity_waiters"
    )
    monkeypatch.setattr(waiters, "callback", lambda function: function)
    hass.services = _FakeCallServices(
        known={
            ("automation", "turn_on"),
            ("automation", "turn_off"),
            ("automation", "trigger"),
        }
    )
    hass.services.async_services = lambda: {
        "automation": {"turn_on": {}, "turn_off": {}, "trigger": {}}
    }
    backend = native.NativeBackend(
        hass, config_backup=backup or AsyncMock(return_value={})
    )
    definition = define_tool(
        backend,
        AutomationWriteTools,
        "ha_config_set_automation",
        annotations={"readOnlyHint": False},
    )
    return Engine(backend, [definition]), owner, hass, registry, writes, tasks


async def test_native_automation_transform_hash_uses_normalized_config_but_cas_and_backup_use_raw_body(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = []

    async def backup(payload: dict, context: CallContext) -> dict:
        captured.append(copy.deepcopy(payload))
        return {}

    engine, owner, _, _, writes, tasks = _setup_automation(
        native, monkeypatch, backup=backup
    )
    before = copy.deepcopy(owner.current[0])
    result = await engine.call(
        "ha_config_set_automation",
        {
            "identifier": "automation.renamed",
            "python_transform": "config['actions'][0]['delay'] = 2",
            "config_hash": compute_config_hash(_normalize_config_for_roundtrip(before)),
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert (
        result.data["automation_id"] == "automation.renamed"
        and result.data["unique_id"] == "uid"
    )
    assert owner.current == [
        {"id": "uid", "alias": "Before", "triggers": [], "actions": [{"delay": 2}]}
    ]
    assert result.data["config_hash"] == compute_config_hash(owner.current[0])
    assert captured[0]["current_config"] == before and captured[0]["backup_id"] == "uid"
    assert len(writes) == 1


@pytest.mark.parametrize(
    "identifier,config",
    [
        ("automation.", {"alias": "Before", "triggers": [], "actions": []}),
        (
            "automation.renamed",
            {"id": "another", "alias": "Before", "triggers": [], "actions": []},
        ),
        (None, {"id": "uid", "alias": "Before", "triggers": [], "actions": []}),
    ],
)
async def test_native_automation_identity_rejections_never_snapshot_or_write(
    native: Any, monkeypatch: pytest.MonkeyPatch, identifier: Any, config: dict
) -> None:
    backup = AsyncMock(return_value={})
    engine, _, _, _, writes, _ = _setup_automation(native, monkeypatch, backup=backup)
    result = await engine.call(
        "ha_config_set_automation",
        {
            "identifier": identifier,
            "config": config,
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert result.is_error and not writes, result.data
    backup.assert_not_awaited()


async def test_native_automation_create_allocates_once_and_waits_for_actual_entity(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = []

    async def backup(payload: dict, context: CallContext) -> dict:
        captured.append(copy.deepcopy(payload))
        return {}

    engine, owner, hass, _, writes, tasks = _setup_automation(
        native, monkeypatch, backup=backup
    )

    async def hook(action: str, key: str) -> None:
        await asyncio.sleep(0)
        hass.states._by_id["automation.assigned"] = FakeState(
            "automation.assigned", id=key
        )
        hass.bus.fire("state_changed", "reload", {"entity_id": "automation.assigned"})

    owner.post_write_hook = hook
    result = await engine.call(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Fresh",
                "triggers": [],
                "actions": [{"delay": 1}],
                "future": {"field": True},
            },
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    key = result.data["unique_id"]
    assert key != "uid" and result.data["operation"] == "created"
    assert (
        result.data["automation_id"] == "automation.assigned"
        and result.data["entity_id"] == "automation.assigned"
    )
    assert owner.current[-1]["id"] == key and owner.current[-1]["future"] == {
        "field": True
    }
    assert (
        len(writes) == 1
        and captured[0]["current_config"] is None
        and captured[0]["backup_id"] is None
    )
    assert not any(hass.bus.listeners.values())


async def test_native_automation_concurrent_typed_change_after_snapshot_is_not_overwritten(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def backup(payload: dict, context: CallContext) -> dict:
        owner.current[0]["future"] = 0
        return {}

    engine, owner, _, _, writes, _ = _setup_automation(
        native, monkeypatch, backup=backup
    )
    owner.current[0]["future"] = False
    before = copy.deepcopy(owner.current[0])
    result = await engine.call(
        "ha_config_set_automation",
        {
            "identifier": "automation.renamed",
            "config": {"alias": "Before", "triggers": [], "actions": []},
            "config_hash": compute_config_hash(_normalize_config_for_roundtrip(before)),
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT", (
        result.data
    )
    assert not writes and type(owner.current[0]["future"]) is int


async def test_native_automation_standalone_enable_then_run_uses_services_and_live_state_only(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = AsyncMock(return_value={})
    engine, owner, hass, _, writes, _ = _setup_automation(
        native, monkeypatch, backup=backup
    )
    owner.read_config = AsyncMock(
        side_effect=AssertionError("Runtime calls must not read config files")
    )

    def applied() -> None:
        hass.states._by_id["automation.renamed"].state = "off"

    hass.services._on_call = applied
    result = await engine.call(
        "ha_config_set_automation",
        {
            "identifier": "uid",
            "enabled": False,
            "run_actions": True,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert (
        result.data["enabled_applied"] is True
        and result.data["actions_triggered"] is True
    )
    assert [call["service"] for call in hass.services.calls] == ["turn_off", "trigger"]
    assert not writes and not any(hass.bus.listeners.values())
    owner.read_config.assert_not_awaited()
    backup.assert_not_awaited()


@pytest.mark.parametrize("confirmed", [True, False])
async def test_native_automation_actions_run_only_after_the_matching_reload(
    native: Any, monkeypatch: pytest.MonkeyPatch, confirmed: bool
) -> None:
    engine, owner, hass, _, writes, tasks = _setup_automation(native, monkeypatch)

    async def hook(action: str, key: str) -> None:
        hass.bus.fire(
            "call_service", "ours", {"domain": "automation", "service": "reload"}
        )
        hass.bus.fire("automation_reloaded", "ours" if confirmed else "other")

    owner.post_write_hook = hook
    monkeypatch.setattr(
        AutomationWriteTools,
        "_config_reload_waiter",
        lambda self, *, enabled: config_reload_waiter(
            self.backend,
            self.context,
            "automation_reloaded",
            enabled=enabled,
            timeout=0.01,
        ),
    )
    result = await engine.call(
        "ha_config_set_automation",
        {
            "identifier": "automation.renamed",
            "config": {"alias": "Before", "triggers": [], "actions": [{"delay": 2}]},
            "run_actions": True,
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error and len(writes) == 1, result.data
    assert result.data["actions_triggered"] is confirmed
    assert [call["service"] for call in hass.services.calls] == (
        ["trigger"] if confirmed else []
    )
    assert not any(hass.bus.listeners.values())


async def test_remote_automation_create_and_transform_reuse_the_existing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient

    monkeypatch.setattr(
        "ha_mcp.config.get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=False, enable_mandatory_bps=False),
    )
    client = HomeAssistantClient.__new__(HomeAssistantClient)
    client.base_url, client.token = "http://synthetic.local", "placeholder"
    stored = {}
    posts = []
    client.get_states = AsyncMock(return_value=[])
    client._poll_for_automation_entity = AsyncMock(return_value="automation.assigned")

    async def request(method: str, path: str, **arguments: Any) -> dict:
        key = path.rsplit("/", 1)[-1]
        if method == "GET":
            if key not in stored:
                raise HomeAssistantAPIError("Not found", status_code=404)
            return copy.deepcopy(stored[key])
        assert method == "POST"
        stored[key] = copy.deepcopy(arguments["json"])
        posts.append((key, copy.deepcopy(stored[key])))
        return {"result": "ok"}

    client._request = request
    backend = RemoteBackend(client)
    definition = define_tool(
        backend,
        AutomationWriteTools,
        "ha_config_set_automation",
        annotations={"readOnlyHint": False},
    )
    engine = Engine(backend, [definition])
    created = await engine.call(
        "ha_config_set_automation",
        {
            "config": {"alias": "Fresh", "triggers": [], "actions": [{"delay": 1}]},
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("remote"),
    )
    assert not created.is_error, created.data
    key = created.data["unique_id"]
    assert (
        created.data["operation"] == "created"
        and created.data["entity_id"] == "automation.assigned"
    )
    transformed = await engine.call(
        "ha_config_set_automation",
        {
            "identifier": key,
            "python_transform": "config['actions'][0]['delay'] = 2",
            "config_hash": compute_config_hash(stored[key]),
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("remote"),
    )
    assert not transformed.is_error, transformed.data
    assert (
        transformed.data["unique_id"] == key
        and transformed.data["operation"] == "updated"
    )
    assert posts[-1][1]["actions"] == [{"delay": 2}] and len(posts) == 2
    client._poll_for_automation_entity.assert_awaited_once_with(key)


async def test_native_automation_blueprint_takeover_preserves_id_and_human_metadata(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .test_native_blueprint_render import (
        _BlueprintFailure,
        _DomainOwner,
        _UndefinedInput,
    )

    engine, owner, hass, _, writes, tasks = _setup_automation(native, monkeypatch)
    owner.current = [
        {
            "id": "uid",
            "alias": "Human",
            "description": "Keep",
            "use_blueprint": {
                "path": "author/example.yaml",
                "input": {"message": "Hello"},
            },
        }
    ]
    renderer = _DomainOwner(hass)
    renderer.domain = "automation"
    renderer.substitute.return_value = {
        "triggers": [],
        "actions": [{"delay": 1}],
        "alias": "Blueprint alias",
        "mode": "queued",
    }
    hass.data["blueprint"] = {"automation": renderer}
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
        "ha_config_set_automation",
        {
            "identifier": "automation.renamed",
            "take_control_of_blueprint": True,
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert result.data["took_control_of_blueprint"] == "author/example.yaml"
    assert owner.current == [
        {
            "id": "uid",
            "alias": "Human",
            "description": "Keep",
            "triggers": [],
            "actions": [{"delay": 1}],
            "mode": "queued",
        }
    ]
    assert len(writes) == 1


async def test_native_automation_saved_config_is_reported_when_followup_service_is_denied(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, owner, hass, _, writes, tasks = _setup_automation(native, monkeypatch)
    monkeypatch.setattr(
        AutomationWriteTools,
        "_config_reload_waiter",
        lambda self, *, enabled: config_reload_waiter(
            self.backend,
            self.context,
            "automation_reloaded",
            enabled=enabled,
            timeout=0.01,
        ),
    )
    hass.services._raises = PermissionError("Service actor was revoked")
    result = await engine.call(
        "ha_config_set_automation",
        {
            "identifier": "automation.renamed",
            "config": {"alias": "Before", "triggers": [], "actions": [{"delay": 2}]},
            "enabled": False,
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error and len(writes) == 1, result.data
    assert owner.current[0]["actions"] == [{"delay": 2}]
    assert (
        result.data["enabled_requested"] is False
        and result.data["enabled_applied"] is False
    )
    assert any("config was written" in warning for warning in result.data["warnings"])
    assert len(hass.services.calls) == 1


async def test_native_automation_saved_create_is_not_rejected_when_registration_read_is_denied(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, owner, _, _, writes, tasks = _setup_automation(native, monkeypatch)
    monkeypatch.setattr(
        AutomationWriteTools,
        "_wait_automation_entity",
        AsyncMock(side_effect=PermissionError("Registration read denied")),
    )
    result = await engine.call(
        "ha_config_set_automation",
        {
            "config": {"alias": "Fresh", "triggers": [], "actions": [{"delay": 1}]},
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error and len(writes) == 1, result.data
    assert owner.current[-1]["id"] == result.data["unique_id"]
    assert result.data["operation"] == "created" and result.data["entity_id"] is None
    assert any("verification failed" in warning for warning in result.data["warnings"])
