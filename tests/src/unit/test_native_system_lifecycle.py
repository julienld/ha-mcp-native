"""Self-affecting requests revalidate, then become Core-owned without a stop cycle."""

import asyncio
import importlib
import sys
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.mutation_guard import MutationAdmission, current_admission
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native


@pytest.fixture
def lifecycle(native: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.system_lifecycle"
    )
    monkeypatch.setattr(sys.modules["homeassistant.core"], "Context", SimpleNamespace)
    tasks = []
    calls = []
    ambient = ContextVar("fixture_request_context", default=None)
    entry = SimpleNamespace(entry_id="self")

    async def call(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs, current_admission.get(), ambient.get()))

    def create_task(coroutine: Any, name: str) -> Any:
        task = asyncio.create_task(coroutine, name=name)
        tasks.append(task)
        return task

    hass = SimpleNamespace(
        services=SimpleNamespace(async_call=AsyncMock(side_effect=call)),
        config_entries=SimpleNamespace(
            async_get_entry=lambda _: entry, async_schedule_reload=lambda _: None
        ),
        async_create_task=create_task,
    )
    manager = SimpleNamespace(
        hass=hass,
        entry=entry,
        accepting=True,
        policy=SimpleNamespace(security_policy=None),
        backend=SimpleNamespace(authorize=AsyncMock()),
        refresh_policy=AsyncMock(),
    )
    owner = module.NativeSystemLifecycle(manager, reply_delay=0)
    return SimpleNamespace(
        owner=owner, manager=manager, calls=calls, tasks=tasks, ambient=ambient
    )


async def _finish(world: Any) -> None:
    await asyncio.wait_for(asyncio.gather(*tuple(world.owner.pending)), 1)
    if world.tasks:
        await asyncio.wait_for(asyncio.gather(*world.tasks), 1)


async def test_restart_reply_precedes_core_handoff_and_clears_private_execution_context(
    lifecycle: Any,
) -> None:
    actor = CallContext("actor", client_id="client", progress=AsyncMock())
    admission = current_admission.set(MutationAdmission(actor, True, lambda: None))
    marker = lifecycle.ambient.set("private request context")
    try:
        lifecycle.owner.schedule("restart", actor)
        assert not lifecycle.calls
        await _finish(lifecycle)
    finally:
        lifecycle.ambient.reset(marker)
        current_admission.reset(admission)
    args, kwargs, inherited_admission, inherited_request = lifecycle.calls[0]
    assert args == ("homeassistant", "restart", {})
    assert kwargs["blocking"] is False and kwargs["context"].user_id == "actor"
    assert inherited_admission is None and inherited_request is None
    assert not lifecycle.owner.pending
    actor.progress.assert_not_awaited()


async def test_credential_revoked_after_reply_prevents_deferred_restart(
    lifecycle: Any, caplog: Any
) -> None:
    lifecycle.manager.backend.authorize.side_effect = PermissionError(
        "credential revoked"
    )
    lifecycle.owner.schedule("restart", CallContext("actor"))
    await _finish(lifecycle)
    assert not lifecycle.calls and "AUTH_FORBIDDEN" in caplog.text


async def test_policy_change_after_approval_does_not_execute_or_request_hidden_second_approval(
    lifecycle: Any, caplog: Any
) -> None:
    async def change() -> None:
        lifecycle.manager.policy.security_policy = SimpleNamespace(
            rule_effect="require_approval", rules=[]
        )

    lifecycle.manager.refresh_policy.side_effect = change
    lifecycle.owner.schedule("restart", CallContext("actor"))
    await _finish(lifecycle)
    assert not lifecycle.calls and "CONFIG_CONFLICT" in caplog.text


async def test_read_only_change_after_reply_is_rechecked_before_handoff(
    lifecycle: Any, caplog: Any
) -> None:
    allowed = True
    actor = CallContext("actor")

    def check() -> None:
        if not allowed:
            raise ToolFailure("READ_ONLY", "Policy changed")

    admission = current_admission.set(MutationAdmission(actor, True, check))
    try:
        lifecycle.owner.schedule("restart", actor)
        allowed = False
        await _finish(lifecycle)
    finally:
        current_admission.reset(admission)
    assert not lifecycle.calls and "READ_ONLY" in caplog.text


async def test_self_reload_handoff_can_stop_its_manager_without_waiting_on_itself(
    lifecycle: Any,
) -> None:
    observed = []

    def reload(entry_id: str) -> None:
        observed.append(
            (entry_id, current_admission.get(), bool(lifecycle.owner.pending))
        )
        lifecycle.tasks.append(asyncio.create_task(lifecycle.owner.stop()))

    lifecycle.manager.hass.config_entries.async_schedule_reload = reload
    lifecycle.owner.schedule(
        "entry_reload", CallContext("actor"), entry=lifecycle.manager.entry
    )
    await _finish(lifecycle)
    assert observed == [("self", None, False)]
    assert lifecycle.owner.closed


async def test_stop_cancels_only_pending_requests_and_refuses_another_schedule(
    lifecycle: Any,
) -> None:
    lifecycle.owner.reply_delay = 60
    lifecycle.owner.schedule("restart", CallContext("actor"))
    with pytest.raises(ToolFailure) as duplicate:
        lifecycle.owner.schedule("restart", CallContext("actor"))
    assert duplicate.value.code == "LIFECYCLE_PENDING"
    await lifecycle.owner.stop()
    assert not lifecycle.calls and not lifecycle.owner.pending
    with pytest.raises(ToolFailure) as closed:
        lifecycle.owner.schedule("restart", CallContext("actor"))
    assert closed.value.code == "INTEGRATION_STOPPING"
