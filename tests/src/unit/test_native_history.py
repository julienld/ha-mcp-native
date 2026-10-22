"""Recorder queries retain Core execution, units and detached result semantics."""

import asyncio
import copy
import importlib
import sys
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.history_tools import HistoryFacade
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native

START = "2024-01-01T00:00:00+00:00"
END = "2024-01-01T01:00:00+00:00"
STAMP = datetime.fromisoformat(START).timestamp()


@pytest.fixture
async def history(native: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    calls, schemas, threads = [], [], []
    world = SimpleNamespace(
        history={
            "sensor.energy": [
                {"s": "1", "lu": STAMP, "a": {"future": "kept"}, "future_field": True},
                {"s": "2", "lu": STAMP + 60},
            ]
        },
        statistics={
            "sensor.energy": [
                {
                    "start": STAMP * 1000,
                    "mean": 7,
                    "last_reset": 123.4,
                    "future_metric": 9,
                }
            ]
        },
        guardrails=False,
    )

    async def execute(function: Any, *args: Any) -> Any:
        def run() -> Any:
            threads.append(threading.get_ident())
            return function(*args)

        return await asyncio.to_thread(run)

    def states(instance: Any, **arguments: Any) -> dict:
        calls.append(("history", arguments, threading.get_ident()))
        return copy.deepcopy(world.history)

    def statistics(
        instance: Any,
        start_time: datetime,
        end_time: datetime,
        statistic_ids: set[str],
        period: str,
        units: Any,
        types: set[Literal["mean", "last_reset", "future_total"]],
    ) -> dict:
        calls.append(
            (
                "statistics",
                {
                    "start_time": start_time,
                    "end_time": end_time,
                    "statistic_ids": statistic_ids,
                    "period": period,
                    "units": units,
                    "types": types,
                },
                threading.get_ident(),
            )
        )
        result = copy.deepcopy(world.statistics)
        if types == {"last_reset"}:
            assert units == {"energy": "Wh"}
            result["sensor.energy"][0]["last_reset"] = STAMP * 1000
        return result

    def schema(value: dict) -> dict:
        schemas.append(value)
        payload = dict(value)
        if value["type"] == "history/history_during_period":
            payload.setdefault("include_start_time_state", True)
        elif payload.get("types") is not None:
            payload["types"] = set(payload["types"])
        if payload.get("future_switch") == "reject":
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Core rejected future_switch",
                status="not_started",
            )
        return payload

    handlers = {
        name: (Mock(side_effect=AssertionError("A WS handler ran")), schema)
        for name in (
            "history/history_during_period",
            "recorder/statistics_during_period",
        )
    }
    hass = SimpleNamespace(
        data={"websocket_api": handlers},
        config=SimpleNamespace(time_zone="America/Toronto"),
        states=SimpleNamespace(get=lambda _: None),
        async_add_executor_job=execute,
    )
    user = authorize(hass)
    metadata = AsyncMock(
        return_value=[
            {
                "statistic_id": "sensor.energy",
                "statistics_unit_of_measurement": "Wh",
                "display_unit_of_measurement": "kWh",
                "unit_class": "energy",
                "future_metadata": True,
            }
        ]
    )
    recorder = SimpleNamespace(async_add_executor_job=execute)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.recorder",
        SimpleNamespace(get_instance=lambda _: recorder),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.recorder.history",
        SimpleNamespace(get_significant_states=states),
    )
    stats_module = SimpleNamespace(
        statistics_during_period=statistics,
        async_list_statistic_ids=metadata,
        _get_unit_converter=lambda *_: SimpleNamespace(
            UNIT_CLASS="energy", VALID_UNITS={"Wh", "kWh", "MWh"}
        ),
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.recorder.statistics", stats_module
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.recorder.websocket_api",
        SimpleNamespace(UNIT_SCHEMA=lambda value: value),
    )
    dates = SimpleNamespace(
        parse_datetime=datetime.fromisoformat,
        as_utc=lambda value: value.astimezone(UTC),
        utcnow=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    monkeypatch.setitem(sys.modules, "homeassistant.util", SimpleNamespace(dt=dates))
    monkeypatch.setitem(sys.modules, "homeassistant.util.dt", dates)
    monkeypatch.setattr(
        sys.modules["homeassistant.core"],
        "valid_entity_id",
        lambda value: "." in value,
        raising=False,
    )
    backend = native.NativeBackend(hass)
    manager = SimpleNamespace(
        hass=hass,
        backend=backend,
        policy=SimpleNamespace(flag=lambda name: world.guardrails),
    )
    binding = importlib.import_module(
        "custom_components.ha_mcp_tools.native_history_tools"
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.history"
    )
    contract = Mock(
        return_value={
            "status": "available",
            "description_complete": False,
            "warnings": ["Core schema is incomplete"],
        }
    )
    monkeypatch.setattr(module, "describe_contract", contract)
    definition = binding.history_definition(manager)
    return SimpleNamespace(
        world=world,
        hass=hass,
        user=user,
        backend=backend,
        definition=definition,
        engine=Engine(backend, [definition]),
        calls=calls,
        schemas=schemas,
        threads=threads,
        handlers=handlers,
        metadata=metadata,
        recorder=recorder,
        stats_module=stats_module,
        module=module,
        contract=contract,
    )


async def _get(history: Any, **arguments: Any) -> Any:
    return await history.engine.call(
        "ha_get_history",
        {
            "entity_ids": "sensor.energy",
            "start_time": START,
            "end_time": END,
            **arguments,
        },
        CallContext("actor", read_only=True),
    )


async def test_history_owner_receives_live_schema_defaults_and_keeps_future_data(
    history: Any,
) -> None:
    result = await _get(
        history,
        minimal_response=False,
        order="asc",
        limit=1,
        core_options={"future_switch": True},
    )
    assert not result.is_error, result.data
    entity = result.data["data"]["entities"][0]
    assert entity["states"][0] == {
        "state": "1",
        "last_updated": "2023-12-31T19:00:00-05:00",
        "last_changed": "2023-12-31T19:00:00-05:00",
        "attributes": {"future": "kept"},
        "future_field": True,
    }
    assert entity["has_more"] and entity["next_offset"] == 1
    assert history.calls[0][1]["include_start_time_state"] is True
    assert history.calls[0][1]["compressed_state_format"] is True
    assert history.calls[0][1]["future_switch"] is True
    assert history.calls[0][2] != threading.get_ident()
    assert all(value != threading.get_ident() for value in history.threads)
    for handler, _schema in history.handlers.values():
        handler.assert_not_called()


async def test_history_summary_and_schema_survive_field_projection(
    history: Any,
) -> None:
    result = await _get(history, summary=True, include_schema=True, fields=["entities"])
    assert not result.is_error, result.data
    assert "states" not in result.data["data"]["entities"][0]
    assert result.data["data"]["entities"][0]["summary"]
    assert "core_contract" in result.data["data"]
    assert result.data["warnings"] == ["Core schema is incomplete"]
    history.contract.assert_called_once_with(
        history.hass, "history/history_during_period"
    )


async def test_history_policy_blocks_before_recorder_and_can_be_disabled(
    history: Any,
) -> None:
    history.world.guardrails = True
    result = await _get(history, start_time="2023-01-01T00:00:00Z")
    assert result.is_error and "safe workload budget" in result.data["error"]["message"]
    assert history.calls == []
    history.world.guardrails = False
    assert not (await _get(history, start_time="2023-01-01T00:00:00Z")).is_error
    assert len(history.calls) == 1


async def test_statistics_uses_core_types_metadata_and_stored_unit_reset_query(
    history: Any,
) -> None:
    result = await _get(
        history, source="statistics", core_options={"units": {"energy": "MWh"}}
    )
    assert not result.is_error, result.data
    entity = result.data["data"]["entities"][0]
    assert (
        entity["unit_of_measurement"] == "MWh"
        and entity["unit_source"] == "core_converter"
    )
    assert entity["statistics_metadata"]["future_metadata"] is True
    assert entity["statistics"][0]["future_metric"] == 9
    assert entity["statistics"][0]["last_reset"] == STAMP * 1000
    assert history.calls[0][1]["types"] == {"mean", "last_reset", "future_total"}
    assert history.calls[1][1]["types"] == {"last_reset"}
    assert history.calls[0][2] != threading.get_ident()
    history.metadata.assert_awaited_once_with(history.hass, {"sensor.energy"})


async def test_statistics_metadata_failure_preserves_values_with_unknown_unit(
    history: Any,
) -> None:
    history.metadata.side_effect = OSError("recorder metadata unavailable")
    result = await _get(history, source="statistics", statistic_types=["mean"])
    assert not result.is_error, result.data
    entity = result.data["data"]["entities"][0]
    assert entity["statistics"][0]["mean"] == 7
    assert entity["unit_of_measurement"] is None
    assert entity["unit_reason"] == "statistics_metadata_unavailable"
    assert "last_reset" not in entity["statistics"][0]
    assert any("last_reset omitted" in warning for warning in result.data["warnings"])


async def test_opaque_default_types_refuse_query_but_explicit_types_still_work(
    history: Any,
) -> None:
    original = history.stats_module.statistics_during_period

    def opaque(*args: Any, **kwargs: Any) -> dict:
        return original(*args, **kwargs)

    history.stats_module.statistics_during_period = opaque
    result = await _get(history, source="statistics")
    assert result.is_error and result.data["error"]["code"] == "UNSUPPORTED_OPERATION"
    assert history.calls == []
    assert not (
        await _get(history, source="statistics", statistic_types=["mean"])
    ).is_error


@pytest.mark.parametrize(
    "options", [{"entity_ids": ["sensor.other"]}, {"future_switch": "reject"}]
)
async def test_core_options_neither_override_query_target_nor_bypass_validator(
    history: Any, options: dict
) -> None:
    result = await _get(history, core_options=options)
    assert result.is_error and history.calls == []


async def test_revocation_during_metadata_prevents_statistics_disclosure(
    history: Any,
) -> None:
    async def metadata(*_args: Any) -> list:
        history.user.is_active = False
        return []

    history.metadata.side_effect = metadata
    result = await _get(history, source="statistics")
    assert result.is_error and result.data["error"]["code"] == "AUTH_FORBIDDEN"
    assert "future_metric" not in str(result.data)


async def test_recorder_thread_is_drained_before_repeated_cancellation_returns(
    history: Any,
) -> None:
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    original = history.recorder.async_add_executor_job

    async def executor(function: Any, *args: Any) -> Any:
        def blocked() -> Any:
            started.set()
            assert release.wait(2)
            try:
                return function(*args)
            finally:
                done.set()

        return await original(blocked)

    history.recorder.async_add_executor_job = executor
    service = history.module.NativeHistory(
        history.hass, CallContext("actor"), authorize=history.backend.authorize
    )
    task = asyncio.create_task(
        HistoryFacade(service.get).ha_get_history(
            "sensor.energy", start_time=START, end_time=END
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert done.is_set()
