"""Event emission uses the admitted actor and never retries an uncertain fire."""

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native


@pytest.fixture
def events(native, monkeypatch):
    from custom_components.ha_mcp_tools.native_event_tools import event_definition

    @dataclass
    class Context:
        user_id: str

    core = sys.modules["homeassistant.core"]
    monkeypatch.setattr(
        sys.modules["homeassistant.const"], "EVENT_STATE_CHANGED", "state_changed", raising=False
    )
    origin = object()
    monkeypatch.setattr(core, "Context", Context)
    monkeypatch.setattr(
        core, "EventOrigin", SimpleNamespace(remote=origin), raising=False
    )
    monkeypatch.setattr(
        core,
        "State",
        SimpleNamespace(from_dict=lambda value: SimpleNamespace(**value)),
        raising=False,
    )
    calls = []

    def fire(event_type, data, actual_origin, context):
        calls.append((event_type, data, actual_origin, context))

    hass = SimpleNamespace(bus=SimpleNamespace(async_fire=fire))
    backend = SimpleNamespace(authorize=AsyncMock())
    manager = SimpleNamespace(hass=hass, backend=backend)
    engine = Engine(backend, [event_definition(manager)])
    return manager, engine, calls, origin


def test_native_event_definition_preserves_the_existing_public_contract(events):
    from .test_native_catalog_contract import _ANNOTATIONS, _changed_inputs

    _, engine, _, _ = events
    baseline = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "docs/native-migration/catalog-baseline.json"
        ).read_text(encoding="utf-8")
    )["tools"]
    old = next(row for row in baseline if row["name"] == "ha_call_event")
    actual = engine.tools["ha_call_event"]
    assert not _changed_inputs(old["inputSchema"], actual.input_schema)
    assert all(
        actual.annotations.get(key) == old["annotations"].get(key)
        for key in _ANNOTATIONS
    )
    assert actual.tags == frozenset(old["_meta"]["fastmcp"]["tags"])
    assert actual.description == old["description"]


async def test_payload_detached_and_event_authority_is_a_fresh_actor_context(events):
    _, engine, calls, origin = events
    data = {"nested": {"value": 42}, "context": {"user_id": "not-the-actor"}}
    result = await engine.call(
        "ha_call_event",
        {"event_type": "custom_event", "data": data},
        CallContext("actor"),
    )
    assert result.status == "confirmed" and not result.is_error
    assert result.data == {
        "success": True,
        "event_type": "custom_event",
        "message": "Event custom_event fired.",
    }
    assert len(calls) == 1
    assert calls[0][2] is origin and calls[0][3].user_id == "actor"
    calls[0][1]["nested"]["value"] = 99
    assert data["nested"]["value"] == 42


async def test_state_changed_payload_uses_core_state_conversion(events):
    _, engine, calls, _ = events
    result = await engine.call(
        "ha_call_event",
        {
            "event_type": "state_changed",
            "data": {"old_state": {"state": "off"}, "new_state": {"state": "on"}},
        },
        CallContext("actor"),
    )
    assert not result.is_error
    assert calls[0][1]["new_state"].state == "on"
    assert calls[0][1]["old_state"].state == "off"


@pytest.mark.parametrize("event_type", ["", " ", "bad/path", "bad\\path"])
async def test_invalid_event_type_never_reaches_bus(events, event_type):
    _, engine, calls, _ = events
    result = await engine.call(
        "ha_call_event", {"event_type": event_type}, CallContext("actor")
    )
    assert result.is_error and result.status == "not_started"
    assert calls == []


async def test_malformed_state_payload_is_rejected_before_emission(events):
    _, engine, calls, _ = events
    result = await engine.call(
        "ha_call_event",
        {"event_type": "state_changed", "data": {"new_state": {"state": "on"}}},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started"
    assert calls == []


async def test_read_only_revoked_during_authorization_prevents_emission(events):
    manager, engine, calls, _ = events

    async def authorize(actor):
        if manager.backend.authorize.await_count == 4:
            engine.read_only = True

    manager.backend.authorize.side_effect = authorize
    result = await engine.call(
        "ha_call_event", {"event_type": "custom_event"}, CallContext("actor")
    )
    assert result.is_error and result.status == "not_started"
    assert calls == []


async def test_bus_failure_after_delivery_is_unknown_and_not_retried(events):
    manager, engine, calls, _ = events
    fire = manager.hass.bus.async_fire

    def fail_after_fire(*arguments):
        fire(*arguments)
        raise RuntimeError("subscriber failed after delivery")

    manager.hass.bus.async_fire = fail_after_fire
    result = await engine.call(
        "ha_call_event", {"event_type": "custom_event"}, CallContext("actor")
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert len(calls) == 1 and len(engine.operations) == 1


async def test_actor_refused_at_dispatch_cannot_emit(events):
    manager, engine, calls, _ = events

    async def authorize(actor):
        if manager.backend.authorize.await_count >= 4:
            raise ToolFailure("AUTH_FORBIDDEN", "Actor revoked", status="not_started")

    manager.backend.authorize.side_effect = authorize
    result = await engine.call(
        "ha_call_event", {"event_type": "custom_event"}, CallContext("actor")
    )
    assert result.is_error and result.status == "not_started"
    assert calls == []
