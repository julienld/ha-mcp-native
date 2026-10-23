"""Thread borrows Core's lazy singleton without cancelling shared initialization."""

import asyncio
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.tool_api import ToolFailure

from .test_component_ws_search import _REAL_VOL
from .test_native_engine_backend import native as native
from .test_native_radios import call
from .test_native_radios import radios as radios


@pytest.fixture
def thread_store(radios: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    radios.hass.config.components.add("thread")
    handler = Mock(
        side_effect=AssertionError("Native validation must not call the WS handler")
    )
    radios.hass.data["websocket_api"]["thread/list_datasets"] = (handler, False)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.websocket_api.messages",
        SimpleNamespace(
            BASE_COMMAND_MESSAGE_SCHEMA=_REAL_VOL.Schema(
                {_REAL_VOL.Required("id"): int}
            )
        ),
    )
    owner = SimpleNamespace(datasets={}, preferred_dataset=None)

    async def load(hass: Any) -> Any:
        hass.data["thread.datasets"] = owner
        return owner

    getter = AsyncMock(side_effect=load)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.thread.dataset_store",
        SimpleNamespace(DATA_STORE="thread.datasets", async_get_store=getter),
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.radio_thread"
    )
    return SimpleNamespace(
        radios=radios, owner=owner, getter=getter, module=module, handler=handler
    )


async def test_cold_thread_read_uses_core_singleton_loader(thread_store: Any) -> None:
    world = thread_store
    result = await call(world.radios, "thread", "list_datasets")

    assert not result.is_error, result.data
    assert result.data["datasets"] == {"datasets": []}
    world.getter.assert_awaited_once_with(world.radios.hass)
    assert world.radios.hass.data["thread.datasets"] is world.owner
    world.handler.assert_not_called()


def test_core_type_only_schema_still_rejects_extra_arguments(thread_store: Any) -> None:
    world = thread_store
    session = world.radios.access.RadioSession(
        world.radios.hass, world.radios.actor, world.radios.authorize
    )
    with pytest.raises(ToolFailure, match="extra keys not allowed") as failed:
        session.validate("thread/list_datasets", {"unexpected": True})
    assert failed.value.status == "not_started"
    world.handler.assert_not_called()


async def test_cancelling_a_waiter_never_cancels_shared_core_loading(
    thread_store: Any,
) -> None:
    world = thread_store
    pending = asyncio.get_running_loop().create_future()
    world.radios.hass.data["thread.datasets"] = pending
    entered = asyncio.Event()

    async def shared_loader(hass: Any) -> Any:
        entered.set()
        return await pending

    world.getter.side_effect = shared_loader
    session = world.radios.access.RadioSession(
        world.radios.hass, world.radios.actor, world.radios.authorize
    )
    task = asyncio.create_task(world.module.dataset_owner(session))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not pending.done()
    world.radios.hass.data["thread.datasets"] = world.owner
    pending.set_result(world.owner)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pending.cancelled()
    assert world.radios.hass.data["thread.datasets"] is world.owner


@pytest.mark.parametrize("change", ["revoke", "replace", "unload"])
async def test_thread_loader_result_is_revalidated_before_returning_data(
    thread_store: Any, change: str
) -> None:
    world = thread_store

    async def changed(hass: Any) -> Any:
        hass.data["thread.datasets"] = object() if change == "replace" else world.owner
        if change == "revoke":
            world.radios.state.active = False
        if change == "unload":
            hass.config.components.remove("thread")
        return world.owner

    world.getter.side_effect = changed
    result = await call(world.radios, "thread", "list_datasets")
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == (
        "AUTH_FORBIDDEN" if change == "revoke" else "CONFIG_CONFLICT"
    )
    assert "datasets" not in result.data


async def test_preferred_dataset_refusal_precedes_core_delete(
    thread_store: Any,
) -> None:
    world = thread_store
    world.owner.datasets["preferred"] = SimpleNamespace(
        to_json=lambda: {"id": "preferred"}
    )
    world.owner.preferred_dataset = "preferred"
    world.owner.async_delete = Mock()
    world.radios.command("thread/delete_dataset")
    result = await call(
        world.radios,
        "thread",
        "delete_dataset",
        params={"dataset_id": "preferred"},
        confirm=True,
    )
    assert result.is_error and result.status == "not_started"
    assert "attempt to remove preferred dataset" in result.data["error"]["message"]
    world.owner.async_delete.assert_not_called()
