"""Ownership and continuation tests for native Core flow ports."""

from __future__ import annotations

import asyncio
import importlib
import sys
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.registry_update_lock import registry_update_lock

importlib.import_module(f"{__package__}.test_component_ws_search")


class UnknownFlow(Exception):
    pass


class InvalidData(Exception):
    schema_errors: ClassVar[dict[str, str]] = {"field": "invalid"}


class UnknownHandler(Exception):
    pass


class UnknownStep(Exception):
    pass


async def test_reconfigure_start_compares_revision_before_core_init(flows: Any) -> None:
    owner = manager()
    entry = SimpleNamespace(modified_at=SimpleNamespace(timestamp=lambda: 2.0))
    backend = flows.NativeFlows(
        SimpleNamespace(
            config_entries=SimpleNamespace(
                flow=owner,
                async_get_entry=lambda _key: entry,
            )
        ),
        AsyncMock(),
    )
    try:
        with pytest.raises(ToolFailure, match="changed after its snapshot"):
            await backend.execute(
                "config.flow.start",
                {
                    "kind": "config",
                    "handler": "demo",
                    "entry_id": "entry",
                    "expected_modified_at": 1.0,
                },
                CallContext("actor"),
            )
        owner.async_init.assert_not_awaited()
    finally:
        await backend.stop()


async def test_entry_lock_wait_is_cancellable_and_owned_abort_ignores_readonly(
    flows: Any,
) -> None:
    owner = manager()
    actor = CallContext("actor")
    backend = flows.NativeFlows(
        SimpleNamespace(config_entries=SimpleNamespace(options=owner)), AsyncMock()
    )
    await backend.execute(
        "config.flow.start", {"kind": "options", "handler": "entry"}, actor
    )
    second = asyncio.create_task(
        backend.execute(
            "config.flow.start", {"kind": "options", "handler": "entry"}, actor
        )
    )
    await asyncio.sleep(0)
    assert owner.async_init.await_count == 1 and not second.done()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await second
    assert (
        backend.starting == 0 and registry_update_lock("config_entry", "entry").locked()
    )
    await backend.execute(
        "config.flow.abort", {"flow_id": "owned"}, CallContext("actor", read_only=True)
    )
    assert (
        not backend.owned and not registry_update_lock("config_entry", "entry").locked()
    )


async def test_waiting_flow_rechecks_expected_revision_after_prior_flow_commits(
    flows: Any,
) -> None:
    owner = manager()
    entry = SimpleNamespace(modified_at=SimpleNamespace(timestamp=lambda: 1.0))
    actor = CallContext("actor")
    backend = flows.NativeFlows(
        SimpleNamespace(
            config_entries=SimpleNamespace(
                options=owner, async_get_entry=lambda key: entry
            )
        ),
        AsyncMock(),
    )
    arguments = {"kind": "options", "handler": "entry", "expected_modified_at": 1.0}
    await backend.execute("config.flow.start", arguments, actor)
    waiting = asyncio.create_task(
        backend.execute("config.flow.start", arguments, actor)
    )
    await asyncio.sleep(0)
    assert owner.async_init.await_count == 1

    async def commit(*args: Any) -> dict:
        entry.modified_at = SimpleNamespace(timestamp=lambda: 2.0)
        return {"type": "create_entry", "flow_id": "owned"}

    owner.async_configure.side_effect = commit
    await backend.execute(
        "config.flow.configure", {"flow_id": "owned", "user_input": {}}, actor
    )
    with pytest.raises(ToolFailure, match="changed after its snapshot"):
        _ = await waiting
    assert owner.async_init.await_count == 1 and backend.starting == 0
    assert (
        not backend.owned and not registry_update_lock("config_entry", "entry").locked()
    )


@pytest.mark.parametrize("previous", ["not_applied", "unknown", "applied"])
async def test_refused_flow_step_preserves_prior_apply_knowledge(previous: str) -> None:
    from ha_mcp.runtime.flows.entry import _OptionsFlowProgress

    progress = _OptionsFlowProgress("entry", apply_status=previous)
    submit = AsyncMock(
        side_effect=ToolFailure("READ_ONLY", "blocked", status="not_started")
    )
    with pytest.raises(ToolFailure):
        await progress.submit(None, "flow", {}, submit_fn=submit)
    assert progress.apply_status == previous


@pytest.mark.parametrize("resume", [False, True])
async def test_cancellation_drains_core_step_and_keeps_result_ownership(
    flows: Any, resume: bool
) -> None:
    owner = manager()
    backend = flows.NativeFlows(
        SimpleNamespace(config_entries=SimpleNamespace(flow=owner)), AsyncMock()
    )
    context = CallContext("actor")
    if resume:
        await backend.execute("config.flow.start", {"handler": "template"}, context)
    entered, release = asyncio.Event(), asyncio.Event()

    async def step(*args: Any, **kwargs: Any) -> dict:
        entered.set()
        await release.wait()
        return {"flow_id": "owned", "type": "create_entry" if resume else "form"}

    if resume:
        owner.async_configure.side_effect = step
    else:
        owner.async_init.side_effect = step
    waiter = asyncio.create_task(
        backend.execute(
            "config.flow.configure" if resume else "config.flow.start",
            {"flow_id": "owned", "user_input": {}}
            if resume
            else {"handler": "template"},
            context,
        )
    )
    await entered.wait()
    waiter.cancel()
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.sleep(0)
    assert not waiter.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        _ = await waiter
    assert bool(backend.owned) is not resume
    await backend.stop()
    assert not backend.owned


@pytest.fixture
def flows(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.flows"
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.data_entry_flow",
        SimpleNamespace(
            UnknownFlow=UnknownFlow,
            InvalidData=InvalidData,
            UnknownHandler=UnknownHandler,
            UnknownStep=UnknownStep,
        ),
    )
    monkeypatch.setattr(
        module,
        "_prepare_flow_result",
        lambda hass, kind, result: {
            key: value for key, value in result.items() if key != "private"
        },
    )
    return module


def manager() -> Any:
    return SimpleNamespace(
        async_init=AsyncMock(
            return_value={"flow_id": "owned", "type": "form", "step_id": "user"}
        ),
        async_configure=AsyncMock(
            return_value={
                "flow_id": "owned",
                "type": "create_entry",
                "private": "never emitted",
            }
        ),
        async_abort=Mock(),
    )


async def test_flow_uses_real_managers_and_public_serializer(flows: Any) -> None:
    owner = manager()
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(flow=owner, options=owner, subentries=owner)
    )
    authorize = AsyncMock()
    backend = flows.NativeFlows(hass, authorize)
    actor = CallContext("user", client_id="client")
    started = await backend.execute(
        "config.flow.start", {"kind": "config", "handler": "template"}, actor
    )
    owner.async_init.assert_awaited_once_with("template", context={"source": "user"})
    assert started["flow_id"] == "owned"
    result = await backend.execute(
        "config.flow.configure",
        {"flow_id": "owned", "user_input": {"name": "Example"}},
        actor,
    )
    owner.async_configure.assert_awaited_once_with("owned", {"name": "Example"})
    assert authorize.await_count == 3
    assert all(args.args == (actor,) for args in authorize.await_args_list)
    assert result == {"flow_id": "owned", "type": "create_entry"}
    assert not backend.owned


@pytest.mark.parametrize(
    "other",
    [CallContext("other", client_id="client"), CallContext("user", client_id="other")],
)
async def test_flow_resume_is_bound_to_actor_and_client(
    flows: Any, other: CallContext
) -> None:
    owner = manager()
    backend = flows.NativeFlows(
        SimpleNamespace(config_entries=SimpleNamespace(flow=owner)), AsyncMock()
    )
    await backend.execute(
        "config.flow.start",
        {"handler": "template"},
        CallContext("user", client_id="client"),
    )
    with pytest.raises(ToolFailure, match="does not own"):
        await backend.execute(
            "config.flow.configure", {"flow_id": "owned", "user_input": {}}, other
        )
    owner.async_configure.assert_not_awaited()


async def test_revoked_actor_cannot_resume(flows: Any) -> None:
    owner = manager()
    authorize = AsyncMock()
    backend = flows.NativeFlows(
        SimpleNamespace(config_entries=SimpleNamespace(flow=owner)), authorize
    )
    actor = CallContext("user")
    await backend.execute("config.flow.start", {"handler": "template"}, actor)
    authorize.side_effect = PermissionError("revoked")
    with pytest.raises(ToolFailure, match="revoked"):
        await backend.execute("config.flow.configure", {"flow_id": "owned"}, actor)
    owner.async_configure.assert_not_awaited()


async def test_flow_cap_expiry_and_stop_only_abort_owned(flows: Any) -> None:
    owner = manager()
    backend = flows.NativeFlows(
        SimpleNamespace(config_entries=SimpleNamespace(flow=owner)),
        AsyncMock(),
        max_flows=1,
        ttl=60,
    )
    actor = CallContext("user")
    await backend.execute("config.flow.start", {"handler": "template"}, actor)
    with pytest.raises(ToolFailure, match="Too many"):
        await backend.execute("config.flow.start", {"handler": "template"}, actor)
    backend.owned["owned"].touched -= 61
    await backend.execute("config.flow.start", {"handler": "template"}, actor)
    owner.async_abort.assert_called_once_with("owned")
    await backend.stop()
    assert owner.async_abort.call_count == 2 and not backend.owned


async def test_subentry_reconfigure_preserves_core_handler_and_context(
    flows: Any,
) -> None:
    owner = manager()
    backend = flows.NativeFlows(
        SimpleNamespace(config_entries=SimpleNamespace(subentries=owner)), AsyncMock()
    )
    await backend.execute(
        "config.flow.start",
        {"kind": "subentry", "handler": ["entry", "sensor"], "subentry_id": "sub"},
        CallContext("user"),
    )
    owner.async_init.assert_awaited_once_with(
        ("entry", "sensor"), context={"source": "reconfigure", "subentry_id": "sub"}
    )
    await backend.stop()


async def test_failed_flow_stop_keeps_ownership_for_retry(flows: Any) -> None:
    owner = manager()
    backend = flows.NativeFlows(
        SimpleNamespace(config_entries=SimpleNamespace(flow=owner)), AsyncMock()
    )
    await backend.execute(
        "config.flow.start", {"handler": "template"}, CallContext("user")
    )
    owner.async_abort.side_effect = RuntimeError("cleanup failed")
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await backend.stop()
    assert "owned" in backend.owned
    owner.async_abort.side_effect = None
    await backend.stop()
    assert not backend.owned


@pytest.mark.parametrize("step", ["init", "configure"])
async def test_core_callback_failure_after_dispatch_is_unknown_without_replay(
    flows: Any, step: str
) -> None:
    owner = manager()
    backend = flows.NativeFlows(
        SimpleNamespace(config_entries=SimpleNamespace(options=owner)), AsyncMock()
    )
    calls = []

    async def write_then_raise(*args: Any, **kwargs: Any) -> Any:
        calls.append("committed")
        raise ValueError("listener failed after commit")

    actor = CallContext("actor")
    try:
        if step == "configure":
            await backend.execute(
                "config.flow.start", {"kind": "options", "handler": "entry"}, actor
            )
            owner.async_configure.side_effect = write_then_raise
            operation, arguments = (
                "config.flow.configure",
                {"flow_id": "owned", "user_input": {}},
            )
        else:
            owner.async_init.side_effect = write_then_raise
            operation, arguments = (
                "config.flow.start",
                {"kind": "options", "handler": "entry"},
            )
        with pytest.raises(ToolFailure) as error:
            await backend.execute(operation, arguments, actor)
        assert error.value.status == "outcome_unknown"
        assert calls == ["committed"]
    finally:
        await backend.stop()
