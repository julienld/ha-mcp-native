"""Native search reads actual owner seams and filters its complete corpus first."""

import asyncio
import copy
import importlib
import sys
import threading
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.search_references import GraphResult
from ha_mcp.runtime.search_tools import SearchFacade
from ha_mcp.tool_api import CallContext
from ha_mcp.visibility.model import VisibilityConfig

from .test_component_ws_search import (
    FakeEntityReg,
    FakeHass,
    FakeRegEntry,
    FakeState,
    wsapi,
)
from .test_native_engine_backend import native as native


class ConfigOwner:
    def __init__(self, rows: dict[str, Any]) -> None:
        self.rows = rows
        self.reads = 0
        self.mutation_lock = asyncio.Lock()

    async def read_config(self, hass: Any) -> Any:
        assert self.mutation_lock.locked()
        self.reads += 1
        return copy.deepcopy(self.rows)

    def _get_value(self, hass: Any, rows: dict[str, Any], key: str) -> Any:
        return rows.get(key)


@pytest.fixture
def search(native: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.search"
    )
    sources = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.search_sources"
    )
    bindings = importlib.import_module(
        "custom_components.ha_mcp_tools.native_search_tools"
    )
    entities = {
        "script.one": FakeRegEntry("script.one", unique_id="persisted-one"),
        "script.two": FakeRegEntry("script.two", unique_id="persisted-two"),
        "light.visible": FakeRegEntry("light.visible"),
        "light.hidden": FakeRegEntry("light.hidden"),
    }
    registry = FakeEntityReg(entities)
    monkeypatch.setattr(
        wsapi, "_resolve_registries", lambda hass: wsapi._RegistryView(entity=registry)
    )
    monkeypatch.setattr(wsapi.er, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "entity_registry", wsapi.er
    )
    hass = FakeHass(
        states=[FakeState("light.visible"), FakeState("light.hidden")],
        data={
            domain: SimpleNamespace(entities=[])
            for domain in ("automation", "script", "scene")
        },
    )
    hass.async_add_executor_job = asyncio.to_thread
    owners = {domain: ConfigOwner({}) for domain in ("automation", "script", "scene")}
    monkeypatch.setattr(sources, "config_view", lambda hass, domain: owners[domain])
    monkeypatch.setattr(
        module.NativeSearch, "_related", lambda self, req: (False, None)
    )
    prep = AsyncMock(return_value={"secret_values": frozenset({"credential-value"})})
    monkeypatch.setattr(module, "_search_prep", prep)
    manager = SimpleNamespace(
        hass=hass,
        backend=SimpleNamespace(
            authorize=AsyncMock(),
            execute=AsyncMock(side_effect=AssertionError("Transport dispatch")),
        ),
        visibility=SimpleNamespace(_config=AsyncMock(return_value=None)),
        policy=SimpleNamespace(flags={}),
    )
    engine = Engine(manager.backend, [bindings.search_definition(manager)])
    return SimpleNamespace(
        engine=engine,
        manager=manager,
        module=module,
        sources=sources,
        owners=owners,
        registry=registry,
        entities=entities,
        prep=prep,
    )


def script(
    search: Any, slug: str, body: dict[str, Any], *, persisted: bool = True
) -> None:
    key = search.entities[f"script.{slug}"].unique_id
    search.manager.hass.data["script"].entities.append(
        SimpleNamespace(
            entity_id=f"script.{slug}", unique_id=key, name="Needle", raw_config=body
        )
    )
    if persisted:
        search.owners["script"].rows[key] = copy.deepcopy(body)


async def test_native_search_uses_one_owner_read_and_never_emits_resolved_yaml_body(
    search: Any,
) -> None:
    script(search, "one", {"alias": "Needle", "future_core_field": False})
    script(
        search,
        "two",
        {"alias": "Needle", "password": "credential-value"},
        persisted=False,
    )
    result = await search.engine.call(
        "ha_search",
        {"query": "needle", "search_types": ["script"], "include_config": True},
        CallContext("actor", read_only=True),
    )
    assert not result.is_error, result.data
    assert search.owners["script"].reads == 1
    by_id = {row["entity_id"]: row for row in result.data["scripts"]}
    assert by_id["script.one"]["config"]["future_core_field"] is False
    assert by_id["script.two"]["config"] is None
    assert by_id["script.two"]["source"] == "yaml"
    assert "credential-value" not in str(result.data)
    probe = await search.engine.call(
        "ha_search",
        {"query": "credential-value", "search_types": ["script"], "exact_match": True},
        CallContext("actor"),
    )
    assert not probe.is_error and probe.data["config_total_matches"] == 0
    search.manager.backend.execute.assert_not_awaited()


async def test_native_search_declutter_precedes_entity_counts_and_pagination(
    search: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    search.manager.visibility._config.return_value = VisibilityConfig(
        enabled=True, deny_entity_ids=["light.hidden"]
    )
    monkeypatch.setattr(search.module, "hidden_from_core", lambda *_: {"light.hidden"})
    result = await search.engine.call(
        "ha_search", {"domain_filter": "light", "limit": 1}, CallContext("actor")
    )
    assert not result.is_error, result.data
    assert result.data["entity_total_matches"] == 1
    assert result.data["entities"][0]["entity_id"] == "light.visible"
    assert result.data["entity_has_more"] is False
    assert "light.hidden" not in str(result.data)
    search.prep.assert_awaited_once()
    assert all(owner.reads == 0 for owner in search.owners.values())


async def test_native_search_works_through_the_real_declutter_engine_guard(
    search: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    visibility = importlib.import_module(
        "custom_components.ha_mcp_tools.native_visibility"
    )
    config = VisibilityConfig(enabled=True, deny_entity_ids=["light.hidden"])
    search.manager.visibility = visibility.NativeVisibility(search.manager)
    search.manager.visibility._config = AsyncMock(return_value=config)
    monkeypatch.setattr(search.module, "hidden_from_core", lambda *_: {"light.hidden"})
    search.engine.policy_guard = lambda name, args, actor, writes: (
        search.manager.visibility.before(name, args, actor)
    )
    search.engine.result_filter = search.manager.visibility.after
    result = await search.engine.call(
        "ha_search", {"domain_filter": "light"}, CallContext("actor")
    )
    assert not result.is_error, result.data
    assert result.data["entity_total_matches"] == 1
    assert "light.hidden" not in str(result.data)


async def test_native_search_enforce_filters_entire_config_before_total_and_reference_merge(
    search: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(search, "one", {"alias": "Needle", "target": "light.hidden"})
    script(search, "two", {"alias": "Needle", "target": "light.visible"})
    search.manager.visibility._config.return_value = VisibilityConfig(
        enabled=True, enforce=True, deny_entity_ids=["light.hidden"]
    )
    monkeypatch.setattr(search.module, "hidden_from_core", lambda *_: {"light.hidden"})
    monkeypatch.setattr(
        search.module.NativeSearch,
        "_related",
        lambda self, req: (
            True,
            GraphResult({"scripts": {"script.one", "script.two"}}),
        ),
    )
    result = await search.engine.call(
        "ha_search",
        {
            "query": "needle",
            "search_types": ["script"],
            "limit": 1,
            "include_config": True,
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["config_total_matches"] == 1
    assert [row["entity_id"] for row in result.data["scripts"]] == ["script.two"]
    assert result.data["scripts"][0]["match_in_references"] is True
    assert "light.hidden" not in str(result.data)


async def test_native_search_reference_hits_are_counted_before_window_even_without_a_body_match(
    search: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(search, "one", {"alias": "Unrelated text"})
    script(search, "two", {"alias": "Another body"})
    monkeypatch.setattr(
        search.module.NativeSearch,
        "_related",
        lambda self, req: (
            True,
            GraphResult({"scripts": {"script.one", "script.two"}}),
        ),
    )
    result = await search.engine.call(
        "ha_search",
        {
            "query": "sensor.target",
            "search_types": ["script"],
            "exact_match": True,
            "limit": 1,
            "offset": 1,
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["config_total_matches"] == 2
    assert len(result.data["scripts"]) == 1
    row = result.data["scripts"][0]
    assert row["entity_id"] == "script.two"
    assert row["script_id"] == "persisted-two"
    assert row["match_in_references"] is True


async def test_native_search_scrub_failure_does_not_scan_config_or_probe_secret_values(
    search: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(search, "one", {"alias": "Needle", "password": "credential-value"})
    search.prep.return_value = {
        "secret_values": frozenset(),
        "secret_scrub_degraded": True,
    }
    result = await search.engine.call(
        "ha_search",
        {
            "query": "credential-value",
            "search_types": ["script"],
            "include_config": True,
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["config_total_matches"] == 0
    assert result.data["partial"] is True
    assert "scrub" in result.data["partial_reason"]
    assert search.owners["script"].reads == 0


@pytest.mark.parametrize("exact", [True, False])
async def test_native_search_cannot_probe_a_known_numeric_secret(
    search: Any, exact: bool
) -> None:
    script(search, "one", {"alias": "Needle", "pin": 1234})
    search.prep.return_value = {"secret_values": frozenset({"1234"})}
    result = await search.engine.call(
        "ha_search",
        {"query": "1234", "search_types": ["script"], "exact_match": exact},
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["config_total_matches"] == 0


async def test_native_search_scrub_failure_cannot_reintroduce_config_through_the_graph(
    search: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    search.prep.return_value = {
        "secret_values": frozenset(),
        "secret_scrub_degraded": True,
    }
    monkeypatch.setattr(
        search.module.NativeSearch,
        "_related",
        lambda self, req: (True, GraphResult({"scripts": {"script.one"}})),
    )
    result = await search.engine.call(
        "ha_search",
        {"query": "sensor.target", "search_types": ["script"]},
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["partial"] is True and "scrub" in result.data["partial_reason"]
    assert result.data["config_total_matches"] == 0 and result.data["scripts"] == []


async def test_native_search_missing_persisted_owner_is_partial_and_never_falls_back_to_remote(
    search: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(search, "one", {"alias": "Needle", "password": "credential-value"})
    monkeypatch.setattr(
        search.sources,
        "config_view",
        lambda *_: (_ for _ in ()).throw(OSError("unavailable")),
    )
    result = await search.engine.call(
        "ha_search",
        {"query": "needle", "search_types": ["script"], "include_config": True},
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["partial"] is True
    assert result.data["scripts"][0]["config"] is None
    assert result.data["scripts"][0]["source"] == "unknown"
    assert "credential-value" not in str(result.data)
    search.manager.backend.execute.assert_not_awaited()


async def test_native_search_stops_loading_other_sources_after_a_view_read_revokes_the_actor(
    search: Any,
) -> None:
    original = search.owners["automation"].read_config

    async def read(hass: Any) -> Any:
        result = await original(hass)
        search.manager.backend.authorize.side_effect = PermissionError("revoked")
        return result

    search.owners["automation"].read_config = read
    result = await search.engine.call(
        "ha_search", {"query": "needle"}, CallContext("actor")
    )
    assert result.is_error and result.data["error"]["code"] == "AUTH_FORBIDDEN"
    assert search.owners["automation"].reads == 1
    assert search.owners["script"].reads == search.owners["scene"].reads == 0


async def test_native_search_dashboards_join_one_window_and_yaml_never_emits_its_body(
    search: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(search, "one", {"alias": "Needle"})
    dashboards = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.search_dashboards"
    )

    class Storage:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def metadata(self) -> list[dict[str, Any]]:
            return [{"url_path": "yaml-panel", "title": "Registry title"}]

        async def read(self, path: str, *, force: bool) -> Any:
            assert force
            return SimpleNamespace(
                url_path=path,
                owner=SimpleNamespace(
                    mode="yaml" if path == "yaml-panel" else "storage"
                ),
                config={"future_view": {"private_title": "Needle"}},
            )

    monkeypatch.setattr(dashboards, "DashboardStorage", Storage)
    result = await search.engine.call(
        "ha_search",
        {
            "query": "needle",
            "search_types": ["script", "dashboard"],
            "include_config": True,
            "limit": 3,
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["config_total_matches"] == 3
    by_path = {row["url_path"]: row for row in result.data["dashboards"]}
    assert by_path["yaml-panel"]["config"] is None
    assert by_path["yaml-panel"]["title"] == "Registry title"
    assert by_path["lovelace"]["config"]["future_view"]["private_title"] == "Needle"
    page = await search.engine.call(
        "ha_search",
        {
            "query": "needle",
            "search_types": ["script", "dashboard"],
            "limit": 1,
            "offset": 2,
        },
        CallContext("actor"),
    )
    assert not page.is_error and page.data["count"] == 1
    assert page.data["config_total_matches"] == 3


async def test_native_search_config_budget_override_does_not_change_dashboard_read_timeout(
    search: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_mcp.runtime.search_request import resolve_search_request

    dashboards = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.search_dashboards"
    )
    budgets = []

    def timeout(value: float) -> Any:
        budgets.append(value)
        return asyncio.timeout(value)

    storage = SimpleNamespace(
        metadata=list,
        read=AsyncMock(return_value=SimpleNamespace(owner=object(), config=None)),
    )
    monkeypatch.setattr(
        dashboards, "DashboardStorage", lambda *_args, **_kwargs: storage
    )
    monkeypatch.setattr(dashboards, "asyncio", SimpleNamespace(timeout=timeout))
    search.manager.policy.flags["individual_config_timeout"] = 17
    validate = search.engine.tools["ha_search"].validate
    assert validate is not None
    for config_budget in (0.001, 30):
        req = resolve_search_request(
            validate(
                {
                    "query": "needle",
                    "search_types": ["dashboard"],
                    "config_time_budget": config_budget,
                }
            )
        )
        await dashboards.load_dashboard_documents(
            search.manager, CallContext("actor"), req, AsyncMock()
        )
    assert budgets == [17, 17]


async def test_native_search_worker_observes_a_detached_image_not_later_core_changes(
    search: Any,
) -> None:
    body = {"alias": "Needle", "nested": {"future_core_key": False}}
    script(search, "one", body)
    entered, release = asyncio.Event(), asyncio.Event()

    async def executor(function: Any, *arguments: Any) -> Any:
        assert isinstance(function.args[0], bytes)
        entered.set()
        await release.wait()
        return function(*arguments)

    search.manager.hass.async_add_executor_job = executor
    task = asyncio.create_task(
        search.engine.call(
            "ha_search",
            {"query": "needle", "search_types": ["script"], "include_config": True},
            CallContext("actor"),
        )
    )
    await entered.wait()
    body["nested"]["future_core_key"] = "changed"
    search.owners["script"].rows["persisted-one"]["nested"]["future_core_key"] = (
        "also changed"
    )
    release.set()
    result = await task
    assert not result.is_error, result.data
    assert result.data["scripts"][0]["config"]["nested"]["future_core_key"] is False


async def test_native_search_scores_the_detached_corpus_outside_the_core_loop(
    search: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    core_search = importlib.import_module(
        "custom_components.ha_mcp_tools.websocket_api.search"
    )
    scorer = importlib.import_module(
        "custom_components.ha_mcp_tools.websocket_api.search_score"
    )
    loop_thread = threading.get_ident()
    threads = []
    original = scorer._text_tier

    def score(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return original(*args, **kwargs)

    monkeypatch.setattr(core_search, "_text_tier", score)
    monkeypatch.setattr(scorer, "_text_tier", score)
    matching = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.search_matching"
    )
    monkeypatch.setattr(matching, "_text_tier", score)
    result = await search.engine.call(
        "ha_search",
        {"query": "visible", "domain_filter": "light"},
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert [row["entity_id"] for row in result.data["entities"]] == ["light.visible"]
    assert threads and all(thread != loop_thread for thread in threads)


@pytest.mark.parametrize("surface", ["entity", "script", "helper", "dashboard"])
async def test_native_search_applies_changed_entry_threshold_to_each_surface_before_totals(
    search: Any, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    if surface == "script":
        script(search, "one", {"alias": "Needle"})
    elif surface in {"entity", "helper"}:
        from .test_component_ws_search import FakeStates

        search.manager.hass.states = FakeStates(
            [
                FakeState(
                    "input_boolean.test" if surface == "helper" else "sensor.test",
                    friendly_name="Needle",
                )
            ]
        )
    else:
        from ha_mcp.runtime.search_component_results import _DashboardLeg

        monkeypatch.setattr(
            search.module,
            "load_dashboard_documents",
            AsyncMock(
                return_value=_DashboardLeg(
                    [
                        {
                            "url_path": "lovelace",
                            "title": "Safe metadata",
                            "mode": "storage",
                            "config": {"title": "Needle"},
                        }
                    ]
                )
            ),
        )
    args = {"query": "nedle", "limit": 1, "exact_match": False}
    if surface != "entity":
        args["search_types"] = [surface]
    loose = await search.engine.call("ha_search", args, CallContext("actor"))
    assert not loose.is_error, loose.data
    total = "entity_total_matches" if surface == "entity" else "config_total_matches"
    assert loose.data[total] == 1
    search.manager.policy.flags["fuzzy_threshold"] = 99
    strict = await search.engine.call("ha_search", args, CallContext("actor"))
    assert not strict.is_error, strict.data
    assert strict.data[total] == 0
    assert strict.data["count"] == 0


async def test_native_search_projects_detached_image_and_drains_repeated_cancellation(
    search: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def executor(function: Any, *arguments: Any) -> Any:
        entered.set()
        await release.wait()
        result = function(*arguments)
        completed.set()
        return result

    search.manager.hass.async_add_executor_job = executor
    task = asyncio.create_task(
        SearchFacade(
            search.module.NativeSearch(search.manager, CallContext("actor"))
        ).ha_search(domain_filter="light")
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done() and not completed.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert completed.is_set()


@pytest.mark.parametrize("change", ["visibility", "authorization"])
async def test_native_search_refuses_a_projection_after_its_boundary_changes(
    search: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    async def executor(function: Any, *arguments: Any) -> Any:
        value = function(*arguments)
        if isinstance(function, partial):
            if change == "visibility":
                search.manager.visibility._config.return_value = VisibilityConfig(
                    enabled=True, deny_entity_ids=["light.hidden"]
                )
                monkeypatch.setattr(
                    search.module, "hidden_from_core", lambda *_: {"light.hidden"}
                )
            else:
                search.manager.backend.authorize.side_effect = PermissionError(
                    "revoked"
                )
        return value

    search.manager.hass.async_add_executor_job = executor
    result = await search.engine.call(
        "ha_search", {"domain_filter": "light"}, CallContext("actor")
    )
    assert result.is_error
    assert result.data["error"]["code"] == (
        "CONFIG_CONFLICT" if change == "visibility" else "AUTH_FORBIDDEN"
    )
