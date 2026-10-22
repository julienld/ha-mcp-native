"""Config reads retain the canonical Core owner, freshness and hash semantics."""

from __future__ import annotations

import asyncio
import copy
import importlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web

from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.config_reads import AutomationReadTools
from ha_mcp.tool_api import CallContext, UnsupportedOperation
from ha_mcp.tools.tool_hints import read_only_hints

from .test_component_ws_search import FakeEntityReg, FakeHass, FakeRegEntry, FakeState
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


class _CoreAutomationView:
    name = "api:config:automation:config"
    url = "/api/config/automation/config/{config_key}"

    def __init__(self) -> None:
        self.mutation_lock = asyncio.Lock()
        self.current = [{"id": "uid", "alias": "Before", "action": [{"delay": 1}]}]
        self.reads = 0

    async def read_config(self, hass: Any) -> list[dict[str, Any]]:
        assert self.mutation_lock.locked(), "Native reader skipped Core's mutation lock"
        self.reads += 1
        return copy.deepcopy(self.current)

    def _get_value(self, hass: Any, current: list[dict[str, Any]], key: str) -> Any:
        return next((row for row in current if row["id"] == key), None)


def _http_handler(view: Any) -> Any:
    async def handle(request: Any) -> Any:
        raise AssertionError(f"Native calls must not invoke {view.name}'s HTTP handler")

    return handle


def _register_owner(monkeypatch: pytest.MonkeyPatch, owner: Any) -> web.Application:
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.config.automation",
        SimpleNamespace(EditAutomationConfigView=_CoreAutomationView),
    )
    app = web.Application()
    app.router.add_route("GET", _CoreAutomationView.url, _http_handler(owner))
    return app


async def test_native_config_uses_fresh_owner_content_and_hashes_before_category(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _CoreAutomationView()
    app = _register_owner(monkeypatch, owner)
    entry = FakeRegEntry(
        "automation.current", unique_id="uid", categories={"automation": "one"}
    )
    registry = FakeEntityReg({entry.entity_id: entry})
    helpers = sys.modules["homeassistant.helpers"]
    monkeypatch.setattr(
        helpers, "entity_registry", SimpleNamespace(async_get=lambda hass: registry)
    )
    monkeypatch.setattr(helpers.config_validation, "entity_id", lambda value: value)
    hass = FakeHass(states=[FakeState("automation.current", id="uid")])
    hass.http = SimpleNamespace(app=app)
    authorize(hass)
    backend = native.NativeBackend(hass)
    definition = define_tool(
        backend,
        AutomationReadTools,
        "ha_config_get_automation",
        annotations=read_only_hints("Get Automation Config", open_world=False),
    )
    engine = Engine(backend, [definition])
    first = await engine.call(
        "ha_config_get_automation", {"identifier": "uid"}, CallContext("actor")
    )
    owner.current[0]["alias"] = "After"
    second = await engine.call(
        "ha_config_get_automation", {"identifier": "uid"}, CallContext("actor")
    )
    entry.categories["automation"] = "two"
    third = await engine.call(
        "ha_config_get_automation", {"identifier": "uid"}, CallContext("actor")
    )
    assert first.data["config"]["alias"] == "Before"
    assert second.data["config"]["alias"] == "After"
    assert first.data["config_hash"] != second.data["config_hash"]
    assert second.data["config_hash"] == third.data["config_hash"]
    assert third.data["config"]["category"] == "two"
    assert third.data["config"]["actions"] == [{"delay": 1}]
    assert "action" not in third.data["config"] and owner.reads == 3


async def test_native_config_refuses_a_route_owned_by_another_view(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _CoreAutomationView()
    owner.name = "a different integration"
    app = _register_owner(monkeypatch, owner)
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.core_views"
    )
    with pytest.raises(UnsupportedOperation):
        module.config_view(SimpleNamespace(http=SimpleNamespace(app=app)), "automation")
    assert owner.reads == 0
