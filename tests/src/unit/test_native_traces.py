"""Trace reads use Core owners, detached snapshots and live caller authorization."""

import asyncio
import importlib
import json
import sys
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.trace_tools import TraceFacade
from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


@pytest.fixture
async def traces(native: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    entity_id = "automation.renamed"
    entries = {entity_id: SimpleNamespace(unique_id="stable-id")}
    states = {
        entity_id: SimpleNamespace(state="on", attributes={"last_triggered": "before"}),
        "script.idle": SimpleNamespace(state="off", attributes={}),
    }
    entities = {
        key: SimpleNamespace(_trace_config={"stored_traces": 5}) for key in states
    }
    component = SimpleNamespace(get_entity=entities.get)
    worker_threads = []

    async def execute(function: Any, *args: Any) -> Any:
        def run() -> Any:
            worker_threads.append(threading.get_ident())
            return function(*args)

        return await asyncio.to_thread(run)

    hass = SimpleNamespace(
        data={
            "trace": {},
            "trace_store": object(),
            "automation": component,
            "script": component,
        },
        states=SimpleNamespace(get=states.get),
        async_add_executor_job=execute,
    )
    user = authorize(hass)
    registry = SimpleNamespace(async_get=entries.get)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "entity_registry",
        SimpleNamespace(async_get=lambda _: registry),
    )
    const = SimpleNamespace(
        DATA_TRACE="trace",
        DATA_TRACE_STORE="trace_store",
        CONF_STORED_TRACES="stored_traces",
    )
    owner = SimpleNamespace(
        async_get_trace=AsyncMock(), async_list_traces=AsyncMock(return_value=[])
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components.trace.const", const)
    monkeypatch.setitem(sys.modules, "homeassistant.components.trace.util", owner)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.automation",
        SimpleNamespace(DATA_COMPONENT="automation"),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.script.const",
        SimpleNamespace(DOMAIN="script"),
    )
    encoded_threads = []

    class CoreEncoder(json.JSONEncoder):
        def default(self, value: Any) -> Any:
            encoded_threads.append(threading.get_ident())
            return {"core_value": value.example}

    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.json"],
        "ExtendedJSONEncoder",
        CoreEncoder,
        raising=False,
    )
    backend = native.NativeBackend(hass)
    binding = importlib.import_module(
        "custom_components.ha_mcp_tools.native_trace_tools"
    )
    manager = SimpleNamespace(hass=hass, backend=backend)
    definition = binding.trace_definition(manager)
    engine = Engine(backend, [definition])
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.traces"
    )
    return SimpleNamespace(
        engine=engine,
        hass=hass,
        user=user,
        owner=owner,
        entries=entries,
        states=states,
        entities=entities,
        definition=definition,
        module=module,
        backend=backend,
        worker_threads=worker_threads,
        encoded_threads=encoded_threads,
    )


async def test_trace_list_resolves_renamed_identity_and_pages_detached_core_results(
    traces: Any,
) -> None:
    traces.owner.async_list_traces.return_value = [
        {"run_id": str(index), "state": "stopped"} for index in range(5)
    ]
    progress = AsyncMock()
    result = await traces.engine.call(
        "ha_get_automation_traces",
        {"automation_id": "automation.renamed", "limit": 2, "offset": 1},
        CallContext("actor", read_only=True, progress=progress),
    )
    assert not result.is_error, result.data
    assert [item["run_id"] for item in result.data["traces"]] == ["3", "2"]
    assert result.data["total_available"] == 5 and result.data["has_more"]
    traces.owner.async_list_traces.assert_awaited_once_with(
        traces.hass, "automation", "automation.stable-id"
    )
    assert traces.worker_threads and all(
        value != threading.get_ident() for value in traces.worker_threads
    )
    assert [call.kwargs["progress"] for call in progress.await_args_list] == [0, 1, 3]


async def test_trace_detail_uses_core_extended_encoder_then_shared_section_projection(
    traces: Any,
) -> None:
    value = SimpleNamespace(example="Core-serialized variable")
    traces.owner.async_get_trace.return_value = {
        "state": "stopped",
        "config": {"alias": "Debug"},
        "context": {"id": "context-id"},
        "logbook_entries": [{"name": "entry"}],
        "trace": {
            "sequence/0": [{"changed_variables": {"value": value}}],
            "sequence/1": [{"changed_variables": {"value": value}}],
        },
    }
    result = await traces.engine.call(
        "ha_get_automation_traces",
        {
            "automation_id": "script.idle",
            "run_id": "run",
            "detailed": True,
            "sections": "actions,context",
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["action_trace"][0]["variables"] == {
        "value": {"core_value": value.example}
    }
    assert "variables" not in result.data["action_trace"][1]
    assert result.data["context"] == {"id": "context-id"}
    assert "config_summary" not in result.data and "logbook_entries" not in result.data
    assert traces.encoded_threads == [threading.get_ident(), threading.get_ident()]
    traces.owner.async_get_trace.assert_awaited_once_with(
        traces.hass, "script.idle", "run"
    )


@pytest.mark.parametrize("storage", [5, 0, None])
async def test_idle_script_diagnostics_use_actual_core_trace_settings(
    traces: Any, storage: int | None
) -> None:
    traces.entities["script.idle"]._trace_config = (
        {"stored_traces": storage} if storage is not None else {}
    )
    result = await traces.engine.call(
        "ha_get_automation_traces",
        {"automation_id": "script.idle"},
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    diagnostics = result.data["diagnostics"]
    assert diagnostics["automation_enabled"] is True
    assert diagnostics["trace_storage_enabled"] is (
        storage > 0 if storage is not None else None
    )
    assert "currently disabled" not in diagnostics["suggestion"]
    if storage == 0:
        assert "Trace storage is disabled for this script" in diagnostics["suggestion"]
    if storage is None:
        assert "could not be read" in diagnostics["suggestion"]


@pytest.mark.parametrize("missing", ["storage", "run"])
async def test_trace_owner_or_run_absence_is_not_fabricated_as_an_empty_success(
    traces: Any, missing: str
) -> None:
    if missing == "storage":
        del traces.hass.data["trace_store"]
    else:
        traces.owner.async_get_trace.side_effect = KeyError("run")
    result = await traces.engine.call(
        "ha_get_automation_traces",
        {"automation_id": "automation.renamed", "run_id": "run"},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == (
        "UNSUPPORTED_OPERATION" if missing == "storage" else "SERVICE_CALL_FAILED"
    )
    if missing == "storage":
        traces.owner.async_get_trace.assert_not_awaited()
    else:
        assert result.data["error"]["message"] == "The trace could not be found"


async def test_revocation_after_formatting_prevents_disclosing_trace_payload(
    traces: Any,
) -> None:
    execute = traces.hass.async_add_executor_job

    async def revoke(function: Any) -> Any:
        result = await execute(function)
        traces.user.is_active = False
        return result

    traces.hass.async_add_executor_job = revoke
    traces.owner.async_list_traces.return_value = [{"run_id": "private-run"}]
    result = await traces.engine.call(
        "ha_get_automation_traces",
        {"automation_id": "automation.renamed"},
        CallContext("actor"),
    )
    assert result.is_error and "private-run" not in json.dumps(result.data)
    assert result.data["error"]["code"] == "AUTH_FORBIDDEN"


async def test_cancellation_waits_for_issued_core_trace_restore_io(traces: Any) -> None:
    started, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def read(*_args: Any) -> list:
        started.set()
        await release.wait()
        drained.set()
        return []

    traces.owner.async_list_traces.side_effect = read
    service = traces.module.NativeTraces(
        traces.hass, CallContext("actor"), authorize=traces.backend.authorize
    )
    task = asyncio.create_task(
        TraceFacade(service.get).ha_get_automation_traces("automation.renamed")
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert drained.is_set()
