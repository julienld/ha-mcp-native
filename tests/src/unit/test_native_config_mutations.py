"""Canonical Core config writes validate, compare and dispatch exactly once."""

import asyncio
import copy
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext

from .test_component_ws_search import FakeHass
from .test_native_catalog_contract_config import (
    _CoreAutomationView,
    _register_owner,
)
from .test_native_catalog_contract_config import (
    _real_aiohttp_router as _real_aiohttp_router,
)
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


class _MutableView(_CoreAutomationView):
    def __init__(self, domain: str) -> None:
        super().__init__()
        self.name = f"api:config:{domain}:config"
        self.url = f"/api/config/{domain}/config/{{config_key}}"
        self.path = f"{domain}s.yaml"
        self.domain = domain
        self.current = (
            {"uid": {"alias": "Before"}}
            if domain == "script"
            else [{"id": "uid", "alias": "Before"}]
        )
        self.key_schema = str
        self.data_validator = (
            AsyncMock(return_value={"default_must_not_be_stored": 42})
            if domain != "scene"
            else None
        )
        self.data_schema = (
            (lambda value: {**value, "default_must_not_be_stored": 42})
            if domain == "scene"
            else None
        )
        self.post_write_hook = AsyncMock()

    def _get_value(self, hass: Any, current: Any, key: str) -> Any:
        if self.domain == "script":
            return current.get(key)
        return super()._get_value(hass, current, key)

    def _write_value(self, hass: Any, current: Any, key: str, value: Any) -> None:
        if self.domain == "script":
            current[key] = value
        else:
            current[:] = [row for row in current if row["id"] != key] + [
                {"id": key, **value}
            ]

    def _delete_value(self, hass: Any, current: Any, key: str) -> None:
        if self.domain == "script":
            current.pop(key)
        else:
            current[:] = [row for row in current if row["id"] != key]


def _mutation_setup(monkeypatch: pytest.MonkeyPatch, domain: str) -> tuple:
    owner = _MutableView(domain)
    hass = FakeHass()
    hass.http = SimpleNamespace(app=_register_owner(monkeypatch, owner, domain))
    hass.config = SimpleNamespace(path=lambda path: "/config/" + path)
    user = authorize(hass)
    writes = []
    tasks = []

    def write(path: str, current: Any) -> None:
        assert owner.mutation_lock.locked()
        writes.append((path, copy.deepcopy(current)))
        owner.current = copy.deepcopy(current)

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.config.view",
        SimpleNamespace(_write=write),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.config.const",
        SimpleNamespace(ACTION_DELETE="delete", ACTION_CREATE_UPDATE="create_update"),
    )

    def create_task(task: Any) -> Any:
        scheduled = asyncio.create_task(task)
        tasks.append(scheduled)
        return scheduled

    hass.async_create_task = create_task
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.config_mutations"
    )
    return owner, hass, user, writes, tasks, module


@pytest.mark.parametrize("domain", ["automation", "script", "scene"])
async def test_native_config_writer_uses_core_validation_lock_and_hook(
    native: Any, monkeypatch: pytest.MonkeyPatch, domain: str
) -> None:
    owner, hass, _, writes, tasks, module = _mutation_setup(monkeypatch, domain)
    owner._get_value(hass, owner.current, "uid")["omitted_old_key"] = "remove me"
    backend = native.NativeBackend(hass)
    mutations = module.NativeConfigMutations(hass, backend.authorize)
    expected = copy.deepcopy(owner._get_value(hass, owner.current, "uid"))
    value = {"alias": "After", "future": {"unknown_to_ha_mcp": True}}
    context = CallContext("actor")
    result = await Engine(backend).call_owned(
        "config-write",
        lambda: mutations.execute(
            f"config.{domain}.write",
            {"storage_key": "uid", "expected_config": expected, "data": value},
            context,
        ),
        context,
        writes=True,
    )
    await asyncio.gather(*tasks)
    assert not result.is_error, result.data
    assert len(writes) == 1 and writes[0][0] == f"/config/{domain}s.yaml"
    stored = owner._get_value(hass, owner.current, "uid")
    assert stored["future"] == {"unknown_to_ha_mcp": True}
    assert "default_must_not_be_stored" not in stored
    assert "omitted_old_key" not in stored
    owner.post_write_hook.assert_awaited_once_with("create_update", "uid")


async def test_native_config_delete_conflict_and_listener_failure_do_not_replay(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, hass, _, writes, tasks, module = _mutation_setup(monkeypatch, "automation")
    backend = native.NativeBackend(hass)
    mutations = module.NativeConfigMutations(hass, backend.authorize)
    context = CallContext("actor")
    expected = copy.deepcopy(owner.current[0])
    owner.current[0]["alias"] = "Concurrent edit"
    engine = Engine(backend)

    async def operation() -> Any:
        return await mutations.execute(
            "config.automation.delete",
            {"storage_key": "uid", "expected_config": expected},
            context,
        )

    conflict = await engine.call_owned("remove-config", operation, context, writes=True)
    assert conflict.is_error and conflict.status == "not_started"
    assert conflict.data["error"]["code"] == "CONFIG_CONFLICT" and writes == []
    expected["alias"] = "Concurrent edit"

    async def write_then_raise(function: Any, *args: Any) -> None:
        function(*args)
        raise OSError("reply failed after the atomic replacement")

    hass.async_add_executor_job = write_then_raise

    # Keep the persisted read independent of the deliberately failing writer.
    async def read_config(hass: Any) -> Any:
        return copy.deepcopy(owner.current)

    owner.read_config = read_config
    unknown = await engine.call_owned("remove-config", operation, context, writes=True)
    assert unknown.is_error and unknown.status == "outcome_unknown"
    assert len(writes) == 1 and owner.current == [] and tasks == []


@pytest.mark.parametrize("refusal", ["invalid", "read_only", "revoked"])
async def test_native_config_validation_await_cannot_bypass_a_fresh_refusal(
    native: Any, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    owner, hass, user, writes, tasks, module = _mutation_setup(
        monkeypatch, "automation"
    )
    backend = native.NativeBackend(hass)
    mutations = module.NativeConfigMutations(hass, backend.authorize)
    engine = Engine(backend)

    async def validate(hass: Any, key: str, value: dict) -> None:
        if refusal == "invalid":
            raise ValueError("Core rejected this action")
        if refusal == "read_only":
            engine.read_only = True
        else:
            user.is_active = False

    owner.data_validator = validate
    context = CallContext("actor")
    result = await engine.call_owned(
        "config-write",
        lambda: mutations.execute(
            "config.automation.write",
            {
                "storage_key": "uid",
                "expected_config": copy.deepcopy(owner.current[0]),
                "data": {"alias": "Forbidden"},
            },
            context,
        ),
        context,
        writes=True,
    )
    assert result.is_error
    assert writes == tasks == [] and owner.current[0]["alias"] == "Before"
