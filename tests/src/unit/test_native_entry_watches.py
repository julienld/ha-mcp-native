"""Core entry streams preserve reload evidence and owned cleanup across revocation."""

import importlib
import json
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_component_helper_collections import FakeCollection, _hass
from .test_native_engine_backend import native as native


def _watcher(monkeypatch: pytest.MonkeyPatch, *, limit: int = 32) -> tuple:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.entry_watches"
    )
    monkeypatch.setattr(module, "callback", lambda function: function)
    monkeypatch.setattr(module, "json_bytes", lambda value: json.dumps(value).encode())
    callbacks = []
    state = {"fail_close": False, "revoked": False}
    entry = SimpleNamespace(
        entry_id="one",
        as_json_fragment={"entry_id": "one", "modified_at": 1, "state": "loaded"},
    )

    def connect(hass: Any, signal: str, function: Any) -> Any:
        assert signal == "entry_changed"
        callbacks.append(function)

        def close() -> None:
            if state["fail_close"]:
                raise RuntimeError("dispatcher cleanup failed")
            callbacks.remove(function)

        return close

    async def authorize(_actor: CallContext) -> None:
        if state["revoked"]:
            raise PermissionError("revoked")

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.config_entries",
        SimpleNamespace(SIGNAL_CONFIG_ENTRY_CHANGED="entry_changed"),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.dispatcher",
        SimpleNamespace(async_dispatcher_connect=connect),
    )
    owner = module.NativeEntryWatches(
        SimpleNamespace(
            config_entries=SimpleNamespace(async_get_entry=lambda key: entry)
        ),
        authorize,
        limit=limit,
    )
    return owner, entry, callbacks, state


async def test_native_entry_watch_ignores_precommit_retries_and_observes_reload(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, entry, callbacks, _ = _watcher(monkeypatch)
    actor = CallContext("actor", client_id="client")
    watch = await owner.execute("config.entries.watch", {"entry_id": "one"}, actor)
    for modified, state in [
        (1, "setup_retry"),
        (2, "loaded"),
        (2, "unload_in_progress"),
        (2, "loaded"),
    ]:
        entry.as_json_fragment.update(modified_at=modified, state=state)
        callbacks[0]("updated", entry)
    observed = await owner.execute(
        "config.entries.observe", {**watch, "entry_id": "one", "timeout": 0.1}, actor
    )
    assert observed == {"entry_id": "one", "modified_at": 2, "state": "loaded"}
    await owner.execute("config.entries.close", watch, actor)
    assert not callbacks and not owner.owned


async def test_revoked_entry_watch_owner_can_cleanup_but_cannot_read_or_close_anothers(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _, callbacks, state = _watcher(monkeypatch)
    actor = CallContext("actor", client_id="client")
    watch = await owner.execute("config.entries.watch", {"entry_id": "one"}, actor)
    state["revoked"] = True
    with pytest.raises(PermissionError, match="revoked"):
        await owner.execute(
            "config.entries.observe", {**watch, "entry_id": "one", "timeout": 0}, actor
        )
    with pytest.raises(ToolFailure, match="does not own"):
        await owner.execute(
            "config.entries.close", watch, CallContext("other", client_id="client")
        )
    await owner.execute("config.entries.close", watch, actor)
    assert not callbacks


async def test_entry_watch_cap_overflow_and_failed_cleanup_are_not_reported_as_confirmed(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, entry, callbacks, state = _watcher(monkeypatch, limit=1)
    actor = CallContext("actor")
    watch = await owner.execute("config.entries.watch", {"entry_id": "one"}, actor)
    with pytest.raises(ToolFailure, match="Too many"):
        await owner.execute("config.entries.watch", {"entry_id": "one"}, actor)
    for index in range(140):
        entry.as_json_fragment.update(
            modified_at=index + 2, state="loaded" if index % 2 else "unload_in_progress"
        )
        callbacks[0]("updated", entry)
    assert owner.owned[watch["watch_id"]].queue.qsize() == 128
    assert (
        await owner.execute(
            "config.entries.observe",
            {**watch, "entry_id": "one", "timeout": 0.01},
            actor,
        )
        is None
    )
    state["fail_close"] = True
    with pytest.raises(RuntimeError, match="dispatcher cleanup failed"):
        await owner.stop()
    assert len(owner.owned) == len(callbacks) == 1
    state["fail_close"] = False
    await owner.stop()
    assert not callbacks and not owner.owned


async def test_native_stop_attempts_every_resource_owner_before_reporting_failure(
    native: Any,
) -> None:
    backend = native.NativeBackend(_hass(FakeCollection()))
    flow_stop = AsyncMock(side_effect=RuntimeError("flow abort unavailable"))
    reload_stop, entry_stop = AsyncMock(), AsyncMock()
    backend._flows = SimpleNamespace(stop=flow_stop)
    backend._reload_watches = SimpleNamespace(stop=reload_stop)
    backend._entry_watches = SimpleNamespace(stop=entry_stop)
    with pytest.raises(RuntimeError, match="native_resources_not_closed"):
        await backend.stop()
    reload_stop.assert_awaited_once()
    entry_stop.assert_awaited_once()
    flow_stop.side_effect = None
    await backend.stop()
    assert flow_stop.await_count == 2
