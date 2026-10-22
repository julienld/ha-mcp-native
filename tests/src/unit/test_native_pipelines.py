"""Assist direct owners retain authority, before-images and uncertain outcomes."""

import asyncio
import copy
import importlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native


class Row:
    def __init__(self, **values):
        self.values = values

    def to_json(self):
        return copy.deepcopy(self.values)


class Store:
    def __init__(self):
        self.data = {
            "one": Row(
                id="one",
                name="Voice",
                conversation_engine="homeassistant",
                conversation_language="*",
                language="en",
                future={"value": 1},
            )
        }
        self.preferred = "one"
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.after: Any = None

    def async_get_preferred_item(self):
        return self.preferred

    def async_set_preferred_item(self, identifier):
        self.calls.append(("prefer", identifier))
        self.preferred = identifier

    async def async_create_item(self, values):
        self.calls.append(("create", copy.deepcopy(values)))
        self.data["created"] = Row(id="created", **values)
        await self._notify()
        return self.data["created"]

    async def async_update_item(self, identifier, values):
        self.calls.append(("update", copy.deepcopy(values)))
        self.data[identifier] = Row(id=identifier, **values)
        await self._notify()
        return self.data[identifier]

    async def _notify(self):
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.after:
            self.after()


@pytest.fixture
def pipelines(native, monkeypatch):
    owner = Store()
    hass = SimpleNamespace(
        config=SimpleNamespace(components={"assist_pipeline", "conversation"}),
        data={
            "assist_pipeline": SimpleNamespace(pipeline_store=owner),
            "conversation_agent": object(),
            "conversation_component": object(),
            "websocket_api": {},
        },
    )
    for command in [
        *(
            "assist_pipeline/pipeline/" + a
            for a in ("create", "update", "get", "list", "set_preferred")
        ),
        "conversation/process",
    ]:
        hass.data["websocket_api"][command] = (object(), copy.deepcopy)
    state = SimpleNamespace(active=True, hook=None, calls=[], agent=object())

    async def authorize(actor):
        if actor.credential:
            await actor.credential()
        if state.hook:
            state.hook()
        if not state.active:
            raise PermissionError("revoked")

    def get_pipeline(_hass, identifier=None):
        return owner.data[identifier or owner.preferred]

    async def converse(_hass, **kwargs):
        state.calls.append(kwargs)
        await owner._notify()
        return SimpleNamespace(
            as_dict=lambda: {
                "response": {
                    "response_type": "action_done",
                    "data": {},
                    "language": "en",
                    "speech": {"plain": {"speech": "Done"}},
                },
                "conversation_id": "followup",
            }
        )

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.assist_pipeline.pipeline",
        SimpleNamespace(
            KEY_ASSIST_PIPELINE="assist_pipeline",
            Pipeline=Row,
            async_get_pipeline=get_pipeline,
            validate_language=copy.deepcopy,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.conversation.agent_manager",
        SimpleNamespace(
            async_get_agent=lambda *_: state.agent, async_converse=converse
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.conversation.const",
        SimpleNamespace(DATA_COMPONENT="conversation_component"),
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.pipeline_store"
    )
    conversation = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.pipeline_conversation"
    )
    monkeypatch.setattr(conversation, "Context", SimpleNamespace)
    binder = importlib.import_module(
        "custom_components.ha_mcp_tools.native_pipeline_tools"
    )
    backend = SimpleNamespace(authorize=authorize)
    actor = CallContext("actor")
    service = module.NativePipelines(hass, actor, authorize=authorize)
    engine = Engine(
        backend,
        [binder.pipeline_definition(SimpleNamespace(hass=hass, backend=backend))],
    )
    return SimpleNamespace(
        owner=owner, hass=hass, state=state, service=service, engine=engine, actor=actor
    )


async def call(pipelines, **arguments):
    return await pipelines.engine.call("ha_manage_pipeline", arguments, pipelines.actor)


async def test_callback_replacement_cannot_be_reported_as_our_update(pipelines):
    pipelines.owner.after = lambda: pipelines.owner.data.update(
        one=Row(id="one", name="Other writer")
    )
    result = await call(pipelines, action="update", pipeline_id="one", name="Requested")
    assert result.is_error and result.status == "outcome_unknown", result


async def test_read_refuses_cold_store_without_cancelling_shared_initialization(
    pipelines,
):
    pending = asyncio.get_running_loop().create_future()
    pipelines.hass.data["assist_pipeline"] = pending
    result = await call(pipelines, action="list")
    assert result.is_error and not pending.cancelled()
    assert not pipelines.owner.calls
    pending.cancel()


async def test_clone_preserves_future_core_fields_and_nullable_clear(pipelines):
    result = await call(
        pipelines,
        action="create",
        name="Clone",
        conversation_engine="homeassistant",
        tts_voice="",
        make_preferred=True,
    )
    assert not result.is_error, result.data
    assert result.data["pipeline"]["future"] == {"value": 1}
    assert result.data["pipeline"]["tts_voice"] is None
    assert pipelines.owner.preferred == result.data["pipeline_id"]


@pytest.mark.parametrize(
    "change", ["actor", "owner", "value", "typed_value", "policy", "credential"]
)
async def test_worker_rechecks_frozen_inputs_and_live_admission(
    pipelines, change, monkeypatch
):
    original = type(pipelines.service)._guard

    def mutate():
        if change == "actor":
            pipelines.state.active = False
        elif change == "owner":
            pipelines.hass.data["assist_pipeline"] = SimpleNamespace(
                pipeline_store=Store()
            )
        elif change in {"value", "typed_value"}:
            pipelines.owner.data["one"].values["future"]["value"] = (
                True if change == "typed_value" else 9
            )
        elif change == "credential":
            raise PermissionError("credential revoked")
        else:
            raise ToolFailure("READ_ONLY", "policy revoked", status="not_started")

    async def guard(self, *args):
        pipelines.state.hook = mutate
        return await original(self, *args)

    monkeypatch.setattr(type(pipelines.service), "_guard", guard)
    with pytest.raises(ToolFailure):
        await pipelines.service._write_pipeline(
            action="update",
            pipeline_id="one",
            base_pipeline_id=None,
            updates={"name": "New"},
            make_preferred=False,
        )
    assert not pipelines.owner.calls


@pytest.mark.parametrize(
    "error", [RuntimeError("after effect"), asyncio.CancelledError()]
)
async def test_core_failure_after_mutation_is_unknown(pipelines, error):
    def fail():
        raise error

    pipelines.owner.after = fail
    result = await call(pipelines, action="update", pipeline_id="one", name="New")
    assert result.is_error and result.status == "outcome_unknown", result
    assert len(pipelines.owner.calls) == 1


async def test_double_client_cancel_drains_worker_before_releasing_mutex(pipelines):
    pipelines.owner.block = True

    async def write(name):
        return await pipelines.service._write_pipeline(
            action="update",
            pipeline_id="one",
            base_pipeline_id=None,
            updates={"name": name},
            make_preferred=False,
        )

    first = asyncio.create_task(write("First"))
    await pipelines.owner.started.wait()
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(write("Second"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    kept_lock = (
        len(pipelines.owner.calls) == 1 and not first.done() and not second.done()
    )
    pipelines.owner.release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    result = await second
    assert kept_lock and result["success"] and len(pipelines.owner.calls) == 2


async def test_process_uses_pipeline_agent_language_and_real_actor_context(pipelines):
    result = await call(
        pipelines, action="process", pipeline_id="one", sentence="turn on lights"
    )
    assert not result.is_error, result.data
    request = pipelines.state.calls[0]
    assert request["context"].user_id == "actor"
    assert request["agent_id"] == "homeassistant" and request["language"] == "en"
    assert (
        result.data["conversation_id"] == "followup" and result.data["speech"] == "Done"
    )


async def test_read_only_process_never_reaches_conversation(pipelines):
    pipelines.actor = CallContext("actor", read_only=True)
    with pytest.raises(ToolFailure, match="Writes are disabled"):
        await call(pipelines, action="process", sentence="turn on lights")
    assert not pipelines.state.calls


@pytest.mark.parametrize(
    "change", ["agent", "pipeline", "credential", "policy", "validator"]
)
async def test_conversation_is_pinned_and_reauthorized_inside_owned_worker(
    pipelines, monkeypatch, change
):
    original = type(pipelines.service)._converse

    async def credential():
        raise PermissionError("expired credential")

    async def converse(self, *args):
        if change == "agent":
            pipelines.state.agent = object()
        elif change == "pipeline":
            pipelines.owner.data["one"].values["name"] = "changed"
        elif change == "credential":
            self.actor = CallContext("actor", credential=credential)
        elif change == "validator":
            pipelines.hass.data["websocket_api"]["conversation/process"] = (
                object(),
                copy.deepcopy,
            )
        else:
            pipelines.engine.read_only = True
        return await original(self, *args)

    monkeypatch.setattr(type(pipelines.service), "_converse", converse)
    result = await call(
        pipelines, action="process", pipeline_id="one", sentence="Turn on lights"
    )
    assert result.is_error and result.status == "not_started", result
    assert not pipelines.state.calls


@pytest.mark.parametrize(
    "error", [RuntimeError("after command"), asyncio.CancelledError()]
)
async def test_conversation_failure_after_dispatch_never_replays_the_sentence(
    pipelines, error
):
    def fail():
        raise error

    pipelines.owner.after = fail
    result = await call(pipelines, action="process", sentence="Turn on lights")
    assert result.is_error and result.status == "outcome_unknown", result
    assert len(pipelines.state.calls) == 1


async def test_revocation_during_core_callback_blocks_followup_preferred_mutation(
    pipelines,
):
    def revoke():
        pipelines.state.active = False

    pipelines.owner.after = revoke
    result = await call(
        pipelines,
        action="create",
        name="Clone",
        conversation_engine="homeassistant",
        make_preferred=True,
    )
    assert result.is_error and result.status == "outcome_unknown", result
    assert pipelines.owner.preferred == "one" and len(pipelines.owner.calls) == 1


async def test_core_validator_rejects_before_dispatch_and_gets_future_fields(pipelines):
    from .test_component_ws_search import _REAL_VOL

    seen = []

    def validate(message):
        seen.append(copy.deepcopy(message))
        raise _REAL_VOL.Invalid("Core rejects proposed pipeline")

    pipelines.hass.data["websocket_api"]["assist_pipeline/pipeline/update"] = (
        object(),
        validate,
    )
    result = await call(pipelines, action="update", pipeline_id="one", name="New")
    assert result.is_error and result.status == "not_started", result
    assert seen[0]["future"] == {"value": 1} and not pipelines.owner.calls


async def test_preferred_literal_is_not_fabricated_into_a_core_alias(pipelines):
    result = await call(pipelines, action="get", pipeline_id="preferred")
    assert result.is_error and not pipelines.owner.calls


async def test_read_actions_remain_available_in_read_only_mode(pipelines):
    pipelines.engine.read_only = True
    for action in ("list", "get"):
        result = await call(pipelines, action=action, pipeline_id="one")
        assert not result.is_error, result
    assert not pipelines.owner.calls


def test_extraction_and_native_signature_preserve_legacy_parameters_and_hints(
    pipelines,
):
    import runpy
    from pathlib import Path

    from ha_mcp.tools.tools_assist_pipeline import AssistPipelineTools

    root = Path(__file__).resolve().parents[3]
    extractor = runpy.run_path(str(root / "scripts/extract_tools.py"))
    public = next(
        tool
        for tool in extractor["extract_tools"]()
        if tool["name"] == "ha_manage_pipeline"
    )
    assert public["name"] == "ha_manage_pipeline"
    legacy = AssistPipelineTools.ha_manage_pipeline
    import inspect

    from ha_mcp.runtime.pipeline_tools import PipelineTools

    assert inspect.signature(legacy) == inspect.signature(
        PipelineTools.ha_manage_pipeline
    )
    definition = pipelines.engine.tools["ha_manage_pipeline"]
    assert (
        definition.annotations["openWorldHint"]
        and definition.annotations["destructiveHint"]
    )
    assert definition.tags == frozenset({"Assist"})


async def test_conversation_agent_can_await_a_pipeline_tool_without_mutex_deadlock(
    pipelines, monkeypatch
):
    agent_module = sys.modules["homeassistant.components.conversation.agent_manager"]
    completed = []

    async def converse(_hass, **_kwargs):
        try:
            result = await asyncio.wait_for(
                pipelines.service._write_pipeline(
                    action="update",
                    pipeline_id="one",
                    base_pipeline_id=None,
                    updates={"name": "Agent update"},
                    make_preferred=False,
                ),
                timeout=0.2,
            )
        except TimeoutError:
            completed.append(False)
        else:
            completed.append(result["success"])
        return SimpleNamespace(
            as_dict=lambda: {"response": {"response_type": "action_done", "data": {}}}
        )

    monkeypatch.setattr(agent_module, "async_converse", converse)
    # Bound the fake agent internally so a regression fails without leaving
    # a lock-owning conversation task alive after the test.
    await call(
        pipelines, action="process", pipeline_id="one", sentence="Change my pipeline"
    )
    assert completed == [True]
    assert pipelines.owner.data["one"].values["name"] == "Agent update"


@pytest.mark.parametrize(
    "action, domain", [("list", "assist_pipeline"), ("process", "conversation")]
)
async def test_absent_optional_pipeline_integrations_refuse_before_import(
    pipelines, monkeypatch, action, domain
):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("homeassistant.components." + domain):
            raise AssertionError("Absent optional integration imported")
        return original(name, *args, **kwargs)

    pipelines.hass.config.components.discard(domain)
    monkeypatch.setattr(builtins, "__import__", guarded)
    result = await call(pipelines, action=action, sentence="Turn on lights")
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert not pipelines.state.calls and not pipelines.owner.calls
