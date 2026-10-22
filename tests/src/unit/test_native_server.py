"""Ownership, identity and admission tests for the Home Assistant native host."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import native_server  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_NATIVE_OWNER_ID,
    DATA_WEBHOOK,
    DATA_WEBHOOK_ID,
    DOMAIN,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_NONE,
)


def manager() -> native_server.NativeServerManager:
    hass = MagicMock()
    hass.data = {}
    hass.auth.async_get_user = AsyncMock(
        return_value=SimpleNamespace(
            id="owner", is_active=True, is_admin=True, system_generated=False
        )
    )
    entry = SimpleNamespace(
        data={DATA_WEBHOOK_ID: "secret", DATA_NATIVE_OWNER_ID: "owner"},
        options={},
        entry_id="entry",
    )
    runtime = native_server.NativeServerManager(hass, entry)
    runtime.backend = SimpleNamespace(authorize=AsyncMock())
    runtime.engine = SimpleNamespace(stop=AsyncMock())
    runtime.refresh_policy = AsyncMock()
    runtime.accepting = True
    runtime._supervisor = MagicMock(done=lambda: False)
    return runtime


async def test_bearer_revalidated_at_execution_and_client_identity_bound() -> None:
    runtime = manager()
    token = SimpleNamespace(
        user=SimpleNamespace(id="alice"), client_id="client-A", id="refresh"
    )
    runtime.hass.auth.async_validate_access_token.return_value = token
    request = SimpleNamespace(headers={"Authorization": "Bearer credential"})
    actor = await runtime.request_context(
        request, {"auth_mode": WEBHOOK_AUTH_HA}, read_only=True
    )
    assert (actor.user_id, actor.client_id, actor.read_only) == (
        "alice",
        "client-A",
        True,
    )
    await actor.credential()
    runtime.hass.auth.async_validate_access_token.return_value = None
    with pytest.raises(PermissionError, match="revoked or expired"):
        await actor.credential()


async def test_secret_capability_uses_explicit_owner_and_dies_with_registration() -> (
    None
):
    runtime = manager()
    cfg = {"auth_mode": WEBHOOK_AUTH_NONE}
    runtime.hass.data = {DOMAIN: {DATA_WEBHOOK: cfg}}
    actor = await runtime.request_context(
        SimpleNamespace(headers={}), cfg, read_only=False
    )
    assert actor.user_id == "owner"
    await actor.credential()
    runtime.hass.data[DOMAIN][DATA_WEBHOOK] = dict(cfg)
    with pytest.raises(PermissionError, match="no longer active"):
        await actor.credential()


async def test_unowned_capability_does_not_choose_an_administrator() -> None:
    runtime = manager()
    runtime.entry.data.pop(DATA_NATIVE_OWNER_ID)
    with pytest.raises(PermissionError, match="Configure a Home Assistant owner"):
        await runtime.request_context(
            SimpleNamespace(headers={}),
            {"auth_mode": WEBHOOK_AUTH_NONE},
            read_only=False,
        )
    runtime.hass.auth.async_get_users.assert_not_called()


async def test_admission_limit_counts_authorization_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = manager()
    runtime.http_limit = 1
    entered = asyncio.Event()
    release = asyncio.Event()

    async def identity(*_args: object, **_kwargs: object) -> object:
        entered.set()
        await release.wait()
        return object()

    runtime.request_context = identity
    monkeypatch.setattr(
        native_server, "serve_asgi", AsyncMock(return_value=SimpleNamespace(status=200))
    )
    request = SimpleNamespace()
    first = asyncio.create_task(runtime.async_handle_http(request, {}))
    await entered.wait()
    second = asyncio.create_task(runtime.async_handle_http(request, {}))
    await asyncio.sleep(0)
    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert (first_result.status, second_result.status) == (200, 429)
    assert not runtime._http_tasks


async def test_lifespan_enters_and_exits_in_same_task_and_keeps_state() -> None:
    runtime = manager()
    tasks = []

    @asynccontextmanager
    async def lifespan(_app: object):
        tasks.append(asyncio.current_task())
        yield {"from_lifespan": 42}
        tasks.append(asyncio.current_task())

    runtime.app = SimpleNamespace(lifespan=lifespan)
    runtime._supervisor = asyncio.create_task(runtime._supervise())
    await runtime._ready.wait()
    assert runtime.lifespan_state == {"from_lifespan": 42}
    await runtime.async_stop()
    assert len(tasks) == 2 and tasks[0] is tasks[1]
    assert runtime._stopped


async def test_cancelled_stop_waiter_does_not_cancel_cleanup() -> None:
    runtime = manager()
    runtime._supervisor = None
    draining = asyncio.Event()
    finish = asyncio.Event()

    async def stop() -> None:
        draining.set()
        await finish.wait()

    runtime.engine.stop = stop
    waiter = asyncio.create_task(runtime.async_stop())
    await draining.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not runtime._cleanup_task.cancelled()
    finish.set()
    await runtime.async_stop()
    assert runtime._stopped


async def test_failed_drain_can_be_retried_without_losing_owned_engine() -> None:
    from custom_components.ha_mcp_tools.native_operations.device_operations import (
        NativeDeviceOperations,
    )

    runtime = manager()
    runtime.device_operations = NativeDeviceOperations()
    runtime._supervisor = None
    runtime.engine.stop = AsyncMock(
        side_effect=[RuntimeError("owned work remains"), None]
    )
    with pytest.raises(RuntimeError, match="owned work remains"):
        await runtime.async_stop()
    assert not runtime._stopped and not runtime.device_operations.closed
    await runtime.async_stop()
    assert runtime._stopped and runtime.engine.stop.await_count == 2
    assert runtime.device_operations.closed


async def test_manager_cancels_deferred_restart_before_draining_the_engine() -> None:
    from custom_components.ha_mcp_tools.native_operations.system_lifecycle import (
        NativeSystemLifecycle,
    )
    from ha_mcp.tool_api import CallContext

    runtime = manager()
    runtime._supervisor = None
    runtime.system_lifecycle = NativeSystemLifecycle(runtime, reply_delay=60)
    runtime.system_lifecycle.schedule("restart", CallContext("owner"))
    pending = next(iter(runtime.system_lifecycle.pending))

    async def drain() -> None:
        assert pending.done(), "A deferred self-restart can race the engine's unload"

    runtime.engine.stop.side_effect = drain
    try:
        await runtime.async_stop()
        assert runtime.system_lifecycle.closed and not runtime.system_lifecycle.pending
        runtime.hass.services.async_call.assert_not_called()
    finally:
        await runtime.system_lifecycle.stop()


def test_strict_bps_uses_shared_ack_protocol_and_strips_key() -> None:
    from custom_components.ha_mcp_tools.native_server_policy import NativePolicy
    from ha_mcp.runtime.best_practices import AcknowledgmentState

    runtime = manager()
    runtime.acknowledgment = AcknowledgmentState()
    runtime.skill_available = True
    runtime.policy = NativePolicy(flags={"enable_strict_mandatory_bps": True})
    rejected = runtime.check_best_practices("ha_config_set_helper", {})
    assert rejected is not None and rejected.status == "not_started"
    key = runtime.acknowledgment.current_key()
    assert key not in str(rejected.data)
    arguments = {"BestPracticeKey": key, "name": "example"}
    assert runtime.check_best_practices("ha_config_set_helper", arguments) is None
    assert arguments == {"name": "example"}


async def test_already_cancelled_lifespan_does_not_prevent_owned_cleanup() -> None:
    runtime = manager()
    runtime._supervisor = asyncio.create_task(asyncio.Event().wait())
    runtime._supervisor.cancel()
    await asyncio.gather(runtime._supervisor, return_exceptions=True)
    await runtime.async_stop()
    assert runtime._stopped
    runtime.engine.stop.assert_awaited_once()


@pytest.mark.parametrize("enabled", [False, True])
async def test_shared_result_filter_redacts_after_visibility_without_changing_outcome(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    from custom_components.ha_mcp_tools.native_server_policy import NativePolicy
    from ha_mcp.runtime import redaction
    from ha_mcp.tool_api import CallContext, ToolResult

    monkeypatch.setattr(redaction, "_known_secrets", {"operator-password"})
    runtime = manager()
    runtime.policy = NativePolicy(flags={"redact_secrets": enabled})
    raw = ToolResult({"secret": "operator-password"}, is_error=True, status="unknown")
    visible = ToolResult(
        {"filtered": "failed with operator-password"},
        is_error=True,
        status="unknown",
        operation_id="operation",
    )
    runtime.visibility = SimpleNamespace(after=AsyncMock(return_value=visible))
    actor = CallContext("owner")
    result = await runtime.filter_result("read", {}, actor, raw)
    runtime.visibility.after.assert_awaited_once_with("read", {}, actor, raw)
    assert result.data == {
        "filtered": "failed with <redacted>"
        if enabled
        else "failed with operator-password"
    }
    assert (
        result.is_error
        and result.status == "unknown"
        and result.operation_id == "operation"
    )
