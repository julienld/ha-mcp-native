"""Abandoning a remote flow must release its bounded ownership slot."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.backends.remote_flow_ops import RemoteFlows
from ha_mcp.tool_api import CallContext, ToolFailure


@pytest.mark.parametrize(
    "error", [RuntimeError("abort failed"), asyncio.CancelledError()]
)
async def test_failed_or_cancelled_abort_releases_slot_without_replay(
    error: BaseException,
) -> None:
    abort = AsyncMock(side_effect=error)
    backend = RemoteFlows(SimpleNamespace(abort_config_flow=abort))
    backend.owned["flow"] = ("config", "actor", "client")
    actor = CallContext("actor", client_id="client")
    with pytest.raises(type(error)):
        await backend.execute("config.flow.abort", {"flow_id": "flow"}, actor)
    assert not backend.owned
    with pytest.raises(ToolFailure, match="does not own"):
        await backend.execute("config.flow.abort", {"flow_id": "flow"}, actor)
    abort.assert_awaited_once_with("flow")


async def test_shutdown_aborts_only_owned_flows_once_and_closes_watches() -> None:
    client = SimpleNamespace(
        start_config_flow=AsyncMock(return_value={"type": "form", "flow_id": "config"}),
        start_options_flow=AsyncMock(
            return_value={"type": "form", "flow_id": "options"}
        ),
        start_config_subentry_flow=AsyncMock(
            return_value={"type": "form", "flow_id": "subentry"}
        ),
        abort_config_flow=AsyncMock(),
        abort_options_flow=AsyncMock(),
        abort_config_subentry_flow=AsyncMock(),
    )
    backend = RemoteBackend(client)
    backend._reload_watches = SimpleNamespace(stop=AsyncMock())
    backend._entry_watches = SimpleNamespace(stop=AsyncMock())
    for kind, handler in [
        ("config", "demo"),
        ("options", "entry"),
        ("subentry", ["entry", "demo"]),
    ]:
        await backend.execute(
            "config.flow.start",
            {"kind": kind, "handler": handler},
            CallContext("actor"),
        )

    await backend.stop()

    assert not backend._flows.owned
    client.abort_config_flow.assert_awaited_once_with("config")
    client.abort_options_flow.assert_awaited_once_with("options")
    client.abort_config_subentry_flow.assert_awaited_once_with("subentry")
    backend._reload_watches.stop.assert_awaited_once()
    backend._entry_watches.stop.assert_awaited_once()
    await backend.stop()
    assert client.abort_config_flow.await_count == 1
    assert client.abort_options_flow.await_count == 1
    assert client.abort_config_subentry_flow.await_count == 1


@pytest.mark.parametrize(
    "error", [RuntimeError("abort failed"), asyncio.CancelledError()]
)
async def test_one_failed_shutdown_abort_does_not_skip_other_flows_or_retry(
    error: BaseException,
) -> None:
    client = SimpleNamespace(
        abort_config_flow=AsyncMock(side_effect=error), abort_options_flow=AsyncMock()
    )
    backend = RemoteBackend(client)
    backend._flows = RemoteFlows(client)
    backend._flows.owned.update(
        {
            "failed": ("config", "actor", "client"),
            "other": ("options", "actor", "client"),
        }
    )

    await backend.stop()
    await backend.stop()

    assert not backend._flows.owned
    client.abort_config_flow.assert_awaited_once_with("failed")
    client.abort_options_flow.assert_awaited_once_with("other")


async def test_cancelled_shutdown_finishes_issued_aborts_without_replay() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def abort(flow_id: str) -> None:
        started.set()
        await release.wait()

    client = SimpleNamespace(abort_config_flow=AsyncMock(side_effect=abort))
    backend = RemoteBackend(client)
    backend._reload_watches = SimpleNamespace(stop=AsyncMock())
    backend._entry_watches = SimpleNamespace(stop=AsyncMock())
    backend._flows = RemoteFlows(client)
    backend._flows.owned["owned"] = ("config", "actor", "client")
    stopping = asyncio.create_task(backend.stop())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        stopping.cancel()
        await asyncio.sleep(0)
        assert not stopping.done()
    finally:
        release.set()
        if not stopping.done():
            stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await stopping
    backend._reload_watches.stop.assert_awaited_once()
    backend._entry_watches.stop.assert_awaited_once()
    await backend.stop()
    assert not backend._flows.owned
    client.abort_config_flow.assert_awaited_once_with("owned")


@pytest.mark.parametrize(
    "failures",
    [
        (RuntimeError("flow cleanup"), None, None),
        (None, RuntimeError("reload cleanup"), None),
        (asyncio.CancelledError(), RuntimeError("reload cleanup"), None),
    ],
)
async def test_shutdown_attempts_every_owner_before_reraising_cleanup_failures(
    failures: tuple[BaseException | None, ...],
) -> None:
    backend = RemoteBackend(SimpleNamespace())
    owners = [SimpleNamespace(stop=AsyncMock(side_effect=error)) for error in failures]
    backend._flows, backend._reload_watches, backend._entry_watches = owners
    expected = [error for error in failures if error is not None]
    error_type = type(expected[0]) if len(expected) == 1 else BaseExceptionGroup

    with pytest.raises(error_type) as raised:
        await backend.stop()

    for owner in owners:
        owner.stop.assert_awaited_once()
    if len(expected) == 1:
        assert raised.value is expected[0]
    else:
        assert len(raised.value.exceptions) == 2
        assert isinstance(raised.value.exceptions[0], asyncio.CancelledError)
        assert raised.value.exceptions[1] is expected[1]
    assert (backend._flows, backend._reload_watches, backend._entry_watches) == tuple(
        owners
    )
