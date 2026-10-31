"""Native and remote removals keep canonical targets and real before-images."""

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
from ha_mcp.runtime.config_removals import (
    AutomationRemovalTools,
    SceneRemovalTools,
    ScriptRemovalTools,
)
from ha_mcp.tool_api import CallContext

from .test_component_ws_search import FakeEntityReg, FakeRegEntry, FakeState
from .test_native_catalog_contract_config import (
    _real_aiohttp_router as _real_aiohttp_router,
)
from .test_native_config_mutations import _mutation_setup
from .test_native_engine_backend import native as native

_OWNERS = {
    "automation": AutomationRemovalTools,
    "script": ScriptRemovalTools,
    "scene": SceneRemovalTools,
}


def _definition(backend: Any, domain: str) -> Any:
    return define_tool(
        backend,
        _OWNERS[domain],
        f"ha_config_remove_{domain}",
        annotations={"readOnlyHint": False},
    )


@pytest.mark.parametrize("domain", ["automation", "script", "scene"])
@pytest.mark.parametrize("concurrent_edit", [False, True])
async def test_native_remove_snapshots_canonical_owner_and_refuses_concurrent_edit(
    native: Any, monkeypatch: pytest.MonkeyPatch, domain: str, concurrent_edit: bool
) -> None:
    owner, hass, _, writes, tasks, _ = _mutation_setup(monkeypatch, domain)
    entity_id = f"{domain}.renamed"
    entry = FakeRegEntry(
        entity_id,
        unique_id="uid",
        platform="homeassistant" if domain == "scene" else domain,
    )
    registry = FakeEntityReg({entity_id: entry})
    helpers = sys.modules["homeassistant.helpers"]
    monkeypatch.setattr(
        helpers, "entity_registry", SimpleNamespace(async_get=lambda hass: registry)
    )
    monkeypatch.setattr(helpers.config_validation, "entity_id", lambda value: value)
    monkeypatch.setattr(
        hass.states,
        "get",
        lambda target: FakeState(entity_id, id="uid") if target == entity_id else None,
    )
    captured = []

    async def backup(payload: dict, context: CallContext) -> dict:
        captured.append(copy.deepcopy(payload))
        assert owner._get_value(hass, owner.current, "uid")["alias"] == "Before"
        if concurrent_edit:
            owner._get_value(hass, owner.current, "uid")["alias"] = "Concurrent edit"
        return {}

    backend = native.NativeBackend(hass, config_backup=backup)
    name = f"ha_config_remove_{domain}"
    parameter = "identifier" if domain == "automation" else f"{domain}_id"
    result = await Engine(backend, [_definition(backend, domain)]).call(
        name, {parameter: entity_id, "wait": False}, CallContext("actor")
    )
    await asyncio.gather(*tasks)
    assert captured == [
        {
            "domain": domain,
            "source_tool": name,
            "backup_id": "uid",
            "current_config": {
                **({"id": "uid"} if domain != "script" else {}),
                "alias": "Before",
            },
        }
    ]
    if concurrent_edit:
        assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT"
        assert (
            writes == []
            and owner._get_value(hass, owner.current, "uid")["alias"]
            == "Concurrent edit"
        )
        owner.post_write_hook.assert_not_awaited()
    else:
        assert not result.is_error, result.data
        assert len(writes) == 1 and owner._get_value(hass, owner.current, "uid") is None
        assert result.data["action"] == "delete"
        assert "_verification_entity_id" not in result.data
        assert result.data[f"{domain}_id"] == (
            entity_id if domain == "automation" else "uid"
        )
        if domain == "automation":
            assert result.data["unique_id"] == "uid"
        owner.post_write_hook.assert_awaited_once_with("delete", "uid")


@pytest.mark.parametrize("domain", ["automation", "script", "scene"])
async def test_remote_remove_reuses_confirmed_storage_key_without_second_resolve(
    monkeypatch: pytest.MonkeyPatch, domain: str
) -> None:
    from ha_mcp.client.rest_client import SceneResolution
    from ha_mcp.tools.tools_config_automations import AutomationConfigTools
    from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

    entity_id = f"{domain}.renamed"
    monkeypatch.setattr(
        "ha_mcp.config.get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=False),
    )
    monkeypatch.setattr(
        AutomationConfigTools,
        "_resolve_automation_entity_id",
        AsyncMock(return_value=entity_id),
    )
    monkeypatch.setattr(
        ConfigSceneTools, "_resolve_scene_entity_id", AsyncMock(return_value=entity_id)
    )
    monkeypatch.setattr(
        "ha_mcp.tools.entity_registration.resolve_entity_id_after_write",
        AsyncMock(return_value=entity_id),
    )
    value = {"id": "uid", "alias": "Before"}
    resolution = SceneResolution(
        storage_key="uid", registry_hit=True, platform="homeassistant"
    )
    read = AsyncMock(
        return_value=value
        if domain == "automation"
        else {f"{domain}_id": "uid", "config": value}
    )
    deleted = AsyncMock(
        return_value={f"{domain}_id": "uid", "operation": "deleted", "result": "ok"}
    )
    client = SimpleNamespace(
        **{f"get_{domain}_config": read, f"delete_{domain}_config": deleted},
        _resolve_scene=AsyncMock(return_value=resolution),
    )
    backend = RemoteBackend(client)
    parameter = "identifier" if domain == "automation" else f"{domain}_id"
    result = await Engine(backend, [_definition(backend, domain)]).call(
        f"ha_config_remove_{domain}",
        {parameter: entity_id, "wait": False},
        CallContext("remote"),
    )
    assert not result.is_error, result.data
    assert read.await_count == 2
    caller = entity_id.removeprefix("script.") if domain == "script" else entity_id
    if domain == "scene":
        deleted.assert_awaited_once_with(caller, resolution=resolution)
    else:
        deleted.assert_awaited_once_with(caller, resolved_id="uid")
