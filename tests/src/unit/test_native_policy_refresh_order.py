"""Concurrent policy readers cannot undo an administrator's completed update."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolDefinition

from .test_native_server import manager


def policy_manager():
    runtime = manager()
    del runtime.refresh_policy  # Exercise the real manager method.
    runtime.mcp = SimpleNamespace(instructions="")
    runtime.settings_ui = SimpleNamespace(
        _io=AsyncMock(
            side_effect=lambda _fn, policy, _names: policy.custom_instructions
        ),
        async_stop=AsyncMock(),
    )
    return runtime


@pytest.mark.parametrize("restriction", ["read_only", "disabled"])
async def test_older_read_cannot_reenable_an_admitted_write(restriction: str) -> None:
    from custom_components.ha_mcp_tools.native_server_policy import NativePolicy

    runtime = policy_manager()
    before_effect = asyncio.Event()
    resume_write = asyncio.Event()
    effects = []

    async def write(_arguments, actor):
        before_effect.set()
        await resume_write.wait()
        check_mutation_admission(actor)
        effects.append("write")
        return {"success": True}

    runtime.engine = Engine(
        runtime.backend, [ToolDefinition("write", "Write", {}, write)]
    )
    pending_write = asyncio.create_task(
        runtime.engine.call("write", {}, CallContext("owner"))
    )
    await before_effect.wait()
    old = NativePolicy(custom_instructions="old")
    latest = NativePolicy(
        read_only=restriction == "read_only",
        tool_states={"write": "disabled"} if restriction == "disabled" else {},
        custom_instructions="latest",
    )
    old_read_started = asyncio.Event()
    return_old_read = asyncio.Event()
    reads = 0

    async def read_policy(*_args):
        nonlocal reads
        reads += 1
        if reads == 1:
            old_read_started.set()
            await return_old_read.wait()
            return old
        return latest

    runtime.hass.async_add_executor_job = read_policy
    old_refresh = asyncio.create_task(runtime.refresh_policy())
    await old_read_started.wait()
    latest_refresh = asyncio.create_task(runtime.refresh_policy())
    # Schedule the newer reader before completing the older worker.
    asyncio.get_running_loop().call_soon(return_old_read.set)
    try:
        await asyncio.gather(old_refresh, latest_refresh)
        resume_write.set()
        result = await pending_write
        assert not effects, "A completed restriction must still guard pending effects"
        assert result.is_error
        assert result.data["error"]["code"] == (
            "READ_ONLY" if restriction == "read_only" else "TOOL_DISABLED"
        )
        assert runtime.policy is latest
        assert runtime.engine.read_only is latest.read_only
        assert runtime.engine.enabled_tools == (
            set() if restriction == "disabled" else {"write"}
        )
        assert runtime.instructions == runtime.mcp.instructions == "latest"
    finally:
        resume_write.set()
        await runtime.engine.stop()
        _ = await pending_write


@pytest.mark.parametrize("phase", ["read", "instructions"])
async def test_cancelled_refresh_releases_next_reader(phase: str) -> None:
    from custom_components.ha_mcp_tools.native_server_policy import NativePolicy

    runtime = policy_manager()
    runtime.engine = Engine(runtime.backend)
    entered = asyncio.Event()
    old = NativePolicy(custom_instructions="old")
    latest = NativePolicy(read_only=True, custom_instructions="latest")
    reads = 0

    async def read_policy(*_args):
        nonlocal reads
        reads += 1
        if reads == 1:
            if phase == "read":
                entered.set()
                await asyncio.Future()
            return old
        return latest

    async def instructions(_fn, policy, _names):
        if phase == "instructions" and policy is old:
            entered.set()
            await asyncio.Future()
        return policy.custom_instructions

    runtime.hass.async_add_executor_job = read_policy
    runtime.settings_ui._io = instructions
    interrupted = asyncio.create_task(runtime.refresh_policy())
    await entered.wait()
    following = asyncio.create_task(runtime.refresh_policy())
    await asyncio.sleep(0)
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await interrupted
    await asyncio.wait_for(following, 1)
    assert runtime.policy is latest
    assert runtime.engine.read_only
    assert runtime.mcp.instructions == "latest"


async def test_shutdown_cancels_refresh_owner_and_waiter_without_deadlock() -> None:
    runtime = policy_manager()
    runtime.engine = Engine(runtime.backend)
    entered = asyncio.Event()

    async def read_policy(*_args):
        entered.set()
        await asyncio.Future()

    runtime.hass.async_add_executor_job = read_policy
    runtime.request_context = AsyncMock(return_value=CallContext("owner"))
    first = asyncio.create_task(runtime.async_handle_http(SimpleNamespace(), {}))
    await entered.wait()
    following = asyncio.create_task(runtime.async_handle_http(SimpleNamespace(), {}))
    await asyncio.sleep(0)
    assert len(runtime._http_tasks) == 2
    runtime._supervisor = None
    await asyncio.wait_for(runtime.async_stop(), 1)
    assert first.cancelled() and following.cancelled()
    assert runtime._stopped and not runtime._http_tasks
