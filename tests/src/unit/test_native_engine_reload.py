"""Reload completion belongs to our Core service call, not a global event."""

import asyncio
import copy
import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime import Engine
from ha_mcp.runtime.config_reload import config_reload_waiter
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_catalog_contract_config import (
    _real_aiohttp_router as _real_aiohttp_router,
)
from .test_native_config_mutations import _mutation_setup
from .test_native_engine_backend import native as native


@pytest.fixture(autouse=True)
def _core_callback(native: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.reload_watches"
    )
    monkeypatch.setattr(module, "callback", lambda function: function)


class Bus:
    def __init__(self) -> None:
        self.listeners: dict[str, list] = {}
        self.fail_cleanup = False

    def async_listen(self, event_type: str, listener: Any) -> Any:
        self.listeners.setdefault(event_type, []).append(listener)

        def remove() -> None:
            if self.fail_cleanup:
                self.fail_cleanup = False
                raise RuntimeError("cleanup blocked once")
            self.listeners[event_type].remove(listener)

        return remove

    def fire(self, event_type: str, context_id: str, data: dict | None = None) -> None:
        event = SimpleNamespace(data=data or {}, context=SimpleNamespace(id=context_id))
        for listener in list(self.listeners.get(event_type, [])):
            listener(event)


@pytest.mark.parametrize("completed", [False, True])
async def test_core_write_hook_completion_alone_never_confirms_reload(
    native: Any, monkeypatch: pytest.MonkeyPatch, completed: bool
) -> None:
    owner, hass, _, writes, tasks, _ = _mutation_setup(monkeypatch, "scene")
    hass.bus = bus = Bus()
    dispatched = asyncio.Event()
    release = asyncio.Event()
    background = []

    async def hook(action: str, key: str) -> None:
        bus.fire(
            "call_service",
            "our-service-context",
            {"domain": "scene", "service": "reload"},
        )
        dispatched.set()

        async def reload_service() -> None:
            await release.wait()
            if completed:
                bus.fire("scene_reloaded", "our-service-context")

        background.append(asyncio.create_task(reload_service()))
        # Mirrors Core's default blocking=False: reload has not finished here.

    owner.post_write_hook = hook
    backend = native.NativeBackend(hass)
    actor = CallContext("actor")

    async def write_and_wait() -> bool:
        async with config_reload_waiter(
            backend, actor, "scene_reloaded", timeout=0.04
        ) as wait:
            bus.fire(
                "call_service", "unrelated", {"domain": "scene", "service": "reload"}
            )
            bus.fire("scene_reloaded", "unrelated")
            await backend.execute(
                "config.scene.write",
                {
                    "storage_key": "uid",
                    "expected_config": copy.deepcopy(owner.current[0]),
                    "data": {"name": "New", "entities": {}},
                },
                actor,
            )
            await dispatched.wait()
            await asyncio.gather(*tasks)
            bus.fire("scene_reloaded", "different-later-write")
            pending = asyncio.create_task(wait())
            await asyncio.sleep(0)
            assert not pending.done()
            release.set()
            result = await pending
            assert result is completed
            return result

    result = await Engine(backend).call_owned(
        "scene-write", write_and_wait, actor, writes=True
    )
    await asyncio.gather(*background)
    assert not result.is_error, result.data
    assert result.data is completed and len(writes) == 1
    assert not backend._reload_watches.watches
    assert not any(bus.listeners.values())


async def test_native_reload_watch_owner_cap_expiry_and_retryable_cleanup(
    native: Any,
) -> None:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.reload_watches"
    )
    bus = Bus()
    watches = module.NativeReloadWatches(
        SimpleNamespace(bus=bus), AsyncMock(), limit=1, ttl=0.01
    )
    actor = CallContext("actor")
    opened = await watches.execute(
        "config.reload.watch", {"event_type": "scene_reloaded"}, actor
    )
    with pytest.raises(ToolFailure, match="Too many"):
        await watches.execute(
            "config.reload.watch", {"event_type": "scene_reloaded"}, actor
        )
    with pytest.raises(ToolFailure, match="does not own"):
        await watches.execute("config.reload.wait", opened, CallContext("other"))
    await asyncio.sleep(0.02)
    assert not watches.watches and not any(bus.listeners.values())
    await watches.execute(
        "config.reload.watch", {"event_type": "scene_reloaded"}, actor
    )
    bus.fail_cleanup = True
    with pytest.raises(RuntimeError, match="blocked once"):
        await watches.stop()
    assert watches.watches
    await watches.stop()
    assert not watches.watches and not any(bus.listeners.values())


async def test_remote_reload_scope_reuses_legacy_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.tools import config_write_helpers

    calls = []
    client = object()

    @asynccontextmanager
    async def legacy(given_client: Any, event_type: str, *, timeout: float) -> Any:
        assert given_client is client
        calls.append(("open", event_type, timeout))
        try:
            yield AsyncMock(return_value=True)
        finally:
            calls.append(("close",))

    monkeypatch.setattr(config_write_helpers, "config_reload_waiter", legacy)
    backend = RemoteBackend(client)
    async with config_reload_waiter(
        backend, CallContext("actor"), "automation_reloaded"
    ) as wait:
        assert await wait() is True
    assert calls == [("open", "automation_reloaded", 10.0), ("close",)]
    assert not backend._reload_watches.watches
