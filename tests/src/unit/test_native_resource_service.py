"""Core resource ownership retains live admission across every scheduling point."""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_component_ws_search import _REAL_VOL as vol
from .test_native_engine_backend import native as native


class CoreResources:
    CREATE_SCHEMA = vol.Schema(
        {
            vol.Required("url"): str,
            vol.Required("res_type"): vol.In(["module", "css", "js", "html"]),
        }
    )
    UPDATE_SCHEMA = vol.Schema(
        {
            vol.Optional("url"): str,
            vol.Optional("res_type"): vol.In(["module", "css", "js", "html"]),
        }
    )

    def __init__(self):
        self.loaded = True
        self.data = {
            "existing": {"id": "existing", "url": "/local/old.js", "type": "module"}
        }
        self.calls = []
        self.loads = 0
        self.fail_write = False
        self.fail_load = False

    async def async_get_info(self):
        self.loads += 1
        self.loaded = True
        if self.fail_load:
            raise ValueError("Core migrated values then failed")
        return {"resources": len(self.data)}

    def async_items(self):
        return list(self.data.values())

    async def _process_create_data(self, fields):
        return {"url": fields["url"], "type": fields["res_type"]}

    async def _update_data(self, current, fields):
        return {
            **current,
            "url": fields.get("url", current["url"]),
            "type": fields.get("res_type", current.get("type")),
        }

    async def async_create_item(self, fields):
        values = await self._process_create_data(fields)
        row = {"id": "created", **values}
        self.calls.append("create")
        self.data[row["id"]] = row
        if self.fail_write:
            raise ValueError("failed after store")
        return row

    async def async_update_item(self, identifier, fields):
        values = await self._update_data(self.data[identifier], fields)
        self.calls.append("update")
        self.data[identifier] = values
        if self.fail_write:
            raise ValueError("failed after store")
        return values

    async def async_delete_item(self, identifier):
        self.calls.append("delete")
        self.data.pop(identifier)
        if self.fail_write:
            raise ValueError("failed after store")


@pytest.fixture
async def resources(native, monkeypatch):
    from custom_components.ha_mcp_tools.native_operations import (
        lovelace_resources as module,
    )
    from custom_components.ha_mcp_tools.native_resource_tools import (
        build_resource_tools,
    )

    owner = CoreResources()
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.lovelace.resources",
        SimpleNamespace(ResourceStorageCollection=CoreResources),
    )
    monkeypatch.setattr(module, "json_bytes", lambda value: json.dumps(value).encode())
    monkeypatch.setattr(module, "_INVALID_ERRORS", (vol.Invalid,))
    user = SimpleNamespace(active=True)

    async def authorize(actor):
        if not user.active:
            raise PermissionError("revoked")

    hass = SimpleNamespace(
        data={"lovelace": SimpleNamespace(resources=owner, resource_mode="storage")},
        async_add_executor_job=lambda fn, *args: asyncio.ensure_future(
            asyncio.to_thread(fn, *args)
        ),
    )
    capture = AsyncMock(return_value=None)
    backend = SimpleNamespace(authorize=authorize)
    manager = SimpleNamespace(
        hass=hass, backend=backend, snapshots=SimpleNamespace(resource_backup=capture)
    )
    engine = Engine(backend, build_resource_tools(manager))
    return manager, engine, owner, user, capture, module


async def test_loaded_resource_reads_and_mutations_call_only_core_owner(resources):
    manager, engine, owner, _user, capture, _module = resources
    actor = CallContext("owner")
    listed = await engine.call("ha_config_list_dashboard_resources", {}, actor)
    assert not listed.is_error and listed.data["resources"][0]["url"] == "/local/old.js"
    created = await engine.call(
        "ha_config_set_dashboard_resource",
        {"content": "export const value = 1;"},
        actor,
    )
    assert not created.is_error and created.data["resource_id"] == "created"
    assert owner.data["created"]["url"].startswith("data:text/javascript;base64,")
    updated = await engine.call(
        "ha_config_set_dashboard_resource",
        {"url": "/local/new.js", "resource_id": "existing"},
        actor,
    )
    assert not updated.is_error
    assert capture.await_args.args[0]["current_resource"]["url"] == "/local/old.js"
    deleted = await engine.call(
        "ha_config_delete_dashboard_resource", {"resource_id": "existing"}, actor
    )
    assert not deleted.is_error
    assert owner.calls == ["create", "update", "delete"] and owner.loads == 0
    assert capture.await_count == 2


async def test_unloaded_resource_listing_requires_write_admission_then_becomes_readonly(
    resources,
):
    _manager, engine, owner, _user, _capture, _module = resources
    owner.loaded = False
    actor = CallContext("owner", read_only=True)
    with pytest.raises(ToolFailure):
        await engine.call("ha_config_list_dashboard_resources", {}, actor)
    assert owner.loads == 0
    loaded = await engine.call(
        "ha_config_list_dashboard_resources", {}, CallContext("owner")
    )
    assert not loaded.is_error and owner.loads == 1
    again = await engine.call("ha_config_list_dashboard_resources", {}, actor)
    assert not again.is_error and owner.loads == 1


@pytest.mark.parametrize("phase", ["initialize", "commit"])
async def test_resource_owned_child_revalidates_after_its_scheduling_boundary(
    resources, monkeypatch, phase
):
    _manager, engine, owner, user, _capture, module = resources
    owner.loaded = phase != "initialize"
    owned = module.await_owned_io

    async def revoked(awaitable):
        if getattr(getattr(awaitable, "cr_code", None), "co_name", None) == "_" + phase:
            user.active = False
        return await owned(awaitable)

    monkeypatch.setattr(module, "await_owned_io", revoked)
    name = (
        "ha_config_list_dashboard_resources"
        if phase == "initialize"
        else "ha_config_set_dashboard_resource"
    )
    arguments = (
        {}
        if phase == "initialize"
        else {"url": "/local/new.js", "resource_id": "existing"}
    )
    result = await engine.call(name, arguments, CallContext("owner"))
    assert result.is_error and result.status == "not_started"
    assert owner.loads == 0 and not owner.calls


async def test_resource_snapshot_interleaving_never_overwrites_a_changed_item(
    resources,
):
    _manager, engine, owner, _user, capture, _module = resources

    async def changed(payload, actor):
        assert payload["current_resource"]["url"] == "/local/old.js"
        owner.data["existing"]["url"] = "/local/human.js"

    capture.side_effect = changed
    result = await engine.call(
        "ha_config_set_dashboard_resource",
        {"url": "/local/new.js", "resource_id": "existing"},
        CallContext("owner"),
    )
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert owner.data["existing"]["url"] == "/local/human.js" and not owner.calls


@pytest.mark.parametrize("load", [False, True])
async def test_resource_core_effect_then_exception_is_uncertain_and_not_replayed(
    resources, load
):
    _manager, engine, owner, _user, _capture, _module = resources
    owner.loaded = not load
    owner.fail_load = load
    owner.fail_write = not load
    name = (
        "ha_config_list_dashboard_resources"
        if load
        else "ha_config_set_dashboard_resource"
    )
    arguments = {} if load else {"url": "/local/new.js", "resource_id": "existing"}
    result = await engine.call(name, arguments, CallContext("owner"))
    assert result.is_error and result.status == "outcome_unknown"
    assert owner.loads == int(load)
    assert len(owner.calls) == int(not load)


async def test_yaml_resources_remain_readable_and_refuse_mutation(resources):
    manager, engine, owner, _user, _capture, _module = resources
    manager.hass.data["lovelace"].resource_mode = "yaml"
    listed = await engine.call(
        "ha_config_list_dashboard_resources", {}, CallContext("owner", read_only=True)
    )
    assert not listed.is_error and listed.data["resource_mode"] == "yaml"
    changed = await engine.call(
        "ha_config_set_dashboard_resource",
        {"url": "/local/new.js"},
        CallContext("owner"),
    )
    assert changed.is_error and "YAML-managed" in changed.data["error"]["message"]
    assert not owner.calls


async def test_core_schema_rejection_does_not_initialize_or_mutate_resources(resources):
    _manager, engine, owner, _user, _capture, _module = resources
    owner.loaded = False
    result = await engine.call(
        "ha_config_set_dashboard_resource",
        {"url": "/local/card.bin", "resource_type": "unknown"},
        CallContext("owner"),
    )
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert owner.loads == 0 and not owner.calls


async def test_resource_reload_after_read_classification_cannot_start_a_migration(
    resources,
):
    _manager, engine, owner, _user, _capture, _module = resources

    async def reload_after_admission(name, args, actor, writes):
        assert writes is False
        owner.loaded = False

    engine.policy_guard = reload_after_admission
    result = await engine.call(
        "ha_config_list_dashboard_resources", {}, CallContext("owner")
    )
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "READ_ONLY"
    assert owner.loads == 0 and not owner.calls
