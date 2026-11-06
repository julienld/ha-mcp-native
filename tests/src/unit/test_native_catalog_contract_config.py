"""Config reads retain the canonical Core owner, freshness and hash semantics."""

from __future__ import annotations

import asyncio
import copy
import importlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.config_reads import (
    AutomationReadTools,
    SceneReadTools,
    ScriptReadTools,
)
from ha_mcp.tool_api import CallContext, UnsupportedOperation
from ha_mcp.tools.tool_hints import read_only_hints

from .test_component_ws_search import (
    FakeEntityReg,
    FakeHass,
    FakeRegEntry,
    FakeState,
    make_view,
    wsapi,
)
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


@pytest.fixture(autouse=True)
def _real_aiohttp_router() -> Any:
    """Exercise real route closures even when embedded tests installed stubs.

    Collection-time embedded stubs replace both aiohttp and yarl. Restore the
    exact table afterwards so this owner-contract test cannot pollute peers.
    """
    roots = ("aiohttp", "yarl")

    def belongs(name: str) -> bool:
        return any(name == root or name.startswith(root + ".") for root in roots)

    saved = {
        name: module for name, module in tuple(sys.modules.items()) if belongs(name)
    }
    for name in saved:
        sys.modules.pop(name, None)
    try:
        importlib.import_module("aiohttp.web")
        yield
    finally:
        for name in tuple(sys.modules):
            if belongs(name):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


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


def _register_owner(
    monkeypatch: pytest.MonkeyPatch, owner: Any, domain: str = "automation"
) -> Any:
    monkeypatch.setitem(
        sys.modules,
        f"homeassistant.components.config.{domain}",
        SimpleNamespace(**{f"Edit{domain.title()}ConfigView": type(owner)}),
    )
    app = importlib.import_module("aiohttp.web").Application()
    app.router.add_route("GET", owner.url, _http_handler(owner))
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


class _CoreScriptView(_CoreAutomationView):
    name = "api:config:script:config"
    url = "/api/config/script/config/{config_key}"

    def _get_value(self, hass: Any, current: list[dict[str, Any]], key: str) -> Any:
        return next((row["config"] for row in current if row["id"] == key), None)


async def test_native_script_returns_the_canonical_key_of_a_renamed_entity(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _CoreScriptView()
    owner.current = [
        {
            "id": "original_key",
            "config": {"alias": "Renamed", "sequence": [{"delay": 2}]},
        }
    ]
    entry = FakeRegEntry(
        "script.renamed", unique_id="original_key", categories={"script": "one"}
    )
    registry = FakeEntityReg({entry.entity_id: entry})
    helpers = sys.modules["homeassistant.helpers"]
    monkeypatch.setattr(
        helpers, "entity_registry", SimpleNamespace(async_get=lambda hass: registry)
    )
    monkeypatch.setattr(helpers.config_validation, "entity_id", lambda value: value)
    hass = FakeHass()
    hass.http = SimpleNamespace(app=_register_owner(monkeypatch, owner, "script"))
    authorize(hass)
    backend = native.NativeBackend(hass)
    definition = define_tool(
        backend,
        ScriptReadTools,
        "ha_config_get_script",
        annotations=read_only_hints("Get Script Config", open_world=False),
    )
    result = await Engine(backend, [definition]).call(
        "ha_config_get_script", {"script_id": "script.renamed"}, CallContext("actor")
    )
    assert not result.is_error
    assert result.data["script_id"] == "original_key"
    assert result.data["config"]["sequence"] == [{"delay": 2}]
    assert result.data["config"]["category"] == "one"


class _CoreSceneView(_CoreAutomationView):
    name = "api:config:scene:config"
    url = "/api/config/scene/config/{config_key}"


async def test_native_scene_discovery_finds_body_values_without_exposing_vendor_config(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _CoreSceneView()
    owner.current = [
        {
            "id": "saved",
            "name": "Movie",
            "entities": {"light.desk": {"effect": "Northern Lights"}},
        }
    ]
    entry = FakeRegEntry("scene.renamed", unique_id="saved", platform="homeassistant")
    vendor = FakeRegEntry("scene.hue", unique_id="vendor", platform="hue")
    view = make_view(entity={entry.entity_id: entry, vendor.entity_id: vendor})
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
    helpers = sys.modules["homeassistant.helpers"]
    monkeypatch.setattr(
        helpers, "entity_registry", SimpleNamespace(async_get=lambda hass: view.entity)
    )
    monkeypatch.setattr(helpers.config_validation, "entity_id", lambda value: value)
    hass = FakeHass(
        states=[
            FakeState("scene.renamed", friendly_name="Movie"),
            FakeState("scene.hue", friendly_name="Hue Relax"),
        ]
    )
    hass.http = SimpleNamespace(app=_register_owner(monkeypatch, owner, "scene"))
    authorize(hass)
    backend = native.NativeBackend(hass)
    definition = define_tool(
        backend,
        SceneReadTools,
        "ha_config_get_scene",
        annotations=read_only_hints("Get or Find Scenes", open_world=False),
    )
    engine = Engine(backend, [definition])
    result = await engine.call(
        "ha_config_get_scene",
        {"query": "northern lights", "search_in_config": True},
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert [row["scene_id"] for row in result.data["scenes"]] == ["saved"]
    assert result.data["scenes"][0]["match_in_config"] is True
    assert "config" not in result.data["scenes"][0]
    assert owner.reads == 1
