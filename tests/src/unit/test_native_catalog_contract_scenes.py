"""Scene writes preserve ownership, metadata and reload-before-activation."""

import asyncio
import copy
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.config_reload import config_reload_waiter
from ha_mcp.runtime.scene_writes import SceneWriteTools
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


def _setup_scene(
    native: Any, monkeypatch: pytest.MonkeyPatch, *, backup: Any = None
) -> tuple:
    owner, hass, _, writes, tasks, _ = _mutation_setup(monkeypatch, "scene")
    owner.current = [
        {
            "id": "uid",
            "name": "Movie",
            "entities": {"light.a": "on", "light.b": {"state": "off"}},
            "metadata": {"light.a": {"name": "A"}, "light.b": {"name": "B"}},
        }
    ]
    entry = FakeRegEntry("scene.renamed", unique_id="uid", platform="homeassistant")
    registry = _ScriptRegistry({entry.entity_id: entry})
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "entity_registry",
        SimpleNamespace(async_get=lambda hass: registry),
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"].config_validation,
        "entity_id",
        lambda value: value,
    )
    hass.states._by_id[entry.entity_id] = FakeState(entry.entity_id)
    hass.bus = Bus()
    hass.services = _FakeCallServices(known={("scene", "turn_on")})
    hass.services.async_services = lambda: {"scene": {"turn_on": {}}}
    backend = native.NativeBackend(
        hass, config_backup=backup or AsyncMock(return_value={})
    )
    definition = define_tool(
        backend,
        SceneWriteTools,
        "ha_config_set_scene",
        annotations={"readOnlyHint": False},
    )
    return Engine(backend, [definition]), owner, hass, registry, writes, tasks


async def test_native_scene_replace_and_transform_keep_snapshot_metadata_and_hash(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = []

    async def backup(payload: dict, context: CallContext) -> dict:
        captured.append(copy.deepcopy(payload))
        return {}

    engine, owner, _, _, writes, tasks = _setup_scene(
        native, monkeypatch, backup=backup
    )
    before = copy.deepcopy(owner.current[0])
    result = await engine.call(
        "ha_config_set_scene",
        {
            "scene_id": "scene.renamed",
            "python_transform": "del config['entities']['light.b']",
            "config_hash": compute_config_hash(before),
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert result.data["scene_id"] == "uid"
    assert result.data["config_hash"] == compute_config_hash(owner.current[0])
    assert owner.current[0]["metadata"] == {"light.a": {"name": "A"}}
    assert captured[0]["current_config"] == before and captured[0]["backup_id"] == "uid"
    replaced = await engine.call(
        "ha_config_set_scene",
        {
            "scene_id": "scene.renamed",
            "config": {"entities": {"light.a": "off"}},
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not replaced.is_error, replaced.data
    assert owner.current == [
        {"id": "uid", "name": "renamed", "entities": {"light.a": "off"}}
    ]
    assert len(writes) == 2 and "operation" not in replaced.data


async def test_native_scene_activate_waits_for_our_actual_reload_context(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, owner, hass, _, writes, tasks = _setup_scene(native, monkeypatch)
    dispatched = asyncio.Event()
    release = asyncio.Event()

    async def hook(action: str, key: str) -> None:
        hass.bus.fire(
            "call_service", "our-context", {"domain": "scene", "service": "reload"}
        )
        dispatched.set()

        async def reload() -> None:
            await release.wait()
            hass.bus.fire("scene_reloaded", "our-context")

        hass.async_create_task(reload())

    owner.post_write_hook = hook
    operation = asyncio.create_task(
        engine.call(
            "ha_config_set_scene",
            {
                "scene_id": "scene.renamed",
                "config": {"name": "Updated", "entities": {}},
                "activate": True,
                "wait": False,
                "MandatoryBPS": False,
            },
            CallContext("actor"),
        )
    )
    try:
        await asyncio.wait_for(dispatched.wait(), 2)
        await asyncio.sleep(0)
        hass.bus.fire("scene_reloaded", "unrelated-context")
        await asyncio.sleep(0)
        assert len(writes) == 1 and not operation.done()
        assert hass.services.calls == []
    finally:
        release.set()
    result = await operation
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert result.data["activated"] is True
    assert len(hass.services.calls) == 1
    assert hass.services.calls[0]["service_data"]["entity_id"] == "scene.renamed"
    assert not any(hass.bus.listeners.values())


async def test_native_standalone_activation_supports_vendor_scenes_without_config_io(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = AsyncMock(return_value={})
    engine, _, hass, registry, writes, _ = _setup_scene(
        native, monkeypatch, backup=backup
    )
    vendor = FakeRegEntry("scene.vendor", unique_id="vendor", platform="hue")
    registry.entities[vendor.entity_id] = vendor
    registry._entries[vendor.entity_id] = vendor
    hass.states._by_id[vendor.entity_id] = FakeState(vendor.entity_id)
    activated = await engine.call(
        "ha_config_set_scene",
        {"scene_id": "scene.vendor", "activate": True, "MandatoryBPS": False},
        CallContext("actor"),
    )
    assert not activated.is_error, activated.data
    assert activated.data["activated"] is True and len(hass.services.calls) == 1
    assert writes == []
    backup.assert_not_awaited()
    edit = await engine.call(
        "ha_config_set_scene",
        {
            "scene_id": "scene.vendor",
            "config": {"entities": {}},
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert edit.is_error and edit.data["error"]["code"] == "CONFIG_NOT_FOUND"
    assert writes == []


async def test_native_scene_empty_resolved_id_cannot_create_a_phantom_item(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, owner, _, _, writes, _ = _setup_scene(native, monkeypatch)
    before = copy.deepcopy(owner.current)
    result = await engine.call(
        "ha_config_set_scene",
        {
            "scene_id": "scene.",
            "config": {"entities": {}},
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert result.is_error and writes == [] and owner.current == before


async def test_native_scene_unconfirmed_reload_saves_but_does_not_activate(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, owner, hass, _, writes, tasks = _setup_scene(native, monkeypatch)

    async def hook(action: str, key: str) -> None:
        hass.bus.fire(
            "call_service", "our-context", {"domain": "scene", "service": "reload"}
        )
        hass.bus.fire("scene_reloaded", "another-context")

    owner.post_write_hook = hook
    monkeypatch.setattr(
        SceneWriteTools,
        "_config_reload_waiter",
        lambda self, *, enabled: config_reload_waiter(
            self.backend, self.context, "scene_reloaded", enabled=enabled, timeout=0.01
        ),
    )
    result = await engine.call(
        "ha_config_set_scene",
        {
            "scene_id": "scene.renamed",
            "config": {"entities": {}},
            "activate": True,
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert result.data["activated"] is False and len(writes) == 1
    assert any("not activated" in warning for warning in result.data["warnings"])
    assert hass.services.calls == [] and not any(hass.bus.listeners.values())


async def test_remote_scene_create_and_transform_reuse_existing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.client.rest_client import (
        HomeAssistantAPIError,
        HomeAssistantClient,
        SceneResolution,
    )

    monkeypatch.setattr(
        "ha_mcp.config.get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=False, enable_mandatory_bps=False),
    )
    monkeypatch.setattr(
        "ha_mcp.tools.search.state.StateMixin._fetch_states_via_component",
        AsyncMock(return_value=None),
    )
    client = HomeAssistantClient.__new__(HomeAssistantClient)
    client.base_url, client.token = "http://synthetic.local", "placeholder"
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": {}}
    )
    client.get_entity_state = AsyncMock(
        side_effect=HomeAssistantAPIError("Not found", status_code=404)
    )
    client._scene_state_exists = AsyncMock(return_value=False)
    stored = {}
    posts = []

    async def resolve(identifier: str) -> Any:
        return SceneResolution("new", bool(stored), "homeassistant" if stored else None)

    client._resolve_scene = resolve

    async def request(method: str, path: str, **arguments: Any) -> dict:
        assert path == "config/scene/config/new"
        if method == "GET":
            if not stored:
                raise HomeAssistantAPIError("Not found", status_code=404)
            return copy.deepcopy(stored)
        assert method == "POST"
        posts.append(copy.deepcopy(arguments["json"]))
        stored.clear()
        stored.update({"id": "new", **arguments["json"]})
        return {"result": "ok"}

    client._request = request
    backend = RemoteBackend(client)
    definition = define_tool(
        backend,
        SceneWriteTools,
        "ha_config_set_scene",
        annotations={"readOnlyHint": False},
    )
    engine = Engine(backend, [definition])
    created = await engine.call(
        "ha_config_set_scene",
        {
            "scene_id": "new",
            "config": {"entities": {"light.a": "on"}},
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("remote"),
    )
    assert not created.is_error, created.data
    transformed = await engine.call(
        "ha_config_set_scene",
        {
            "scene_id": "scene.renamed",
            "python_transform": "config['entities']['light.a'] = 'off'",
            "config_hash": compute_config_hash(stored),
            "wait": False,
            "MandatoryBPS": False,
        },
        CallContext("remote"),
    )
    assert not transformed.is_error, transformed.data
    assert transformed.data["scene_id"] == "new" and "operation" not in transformed.data
    assert transformed.data["config_hash"] == compute_config_hash(stored)
    assert len(posts) == 2 and stored["entities"] == {"light.a": "off"}
