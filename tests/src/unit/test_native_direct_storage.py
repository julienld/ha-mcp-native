"""Native helpers call Core's original API without replaying its commit protocol."""

import asyncio
import copy
import sys
from types import SimpleNamespace

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_component_helper_collections import FakeRegistry, ItemNotFound, _hass
from .test_component_ws_search import wsapi
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native
from .test_native_helper_mutations import CoreStorage


class ImmediateStorage(CoreStorage):
    async def _process_create_data(self, fields):
        return dict(fields)

    async def _update_data(self, current, fields):
        return {**current, **fields}


@pytest.fixture
async def direct(native, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.collection",
        SimpleNamespace(
            StorageCollection=CoreStorage,
            DictStorageCollection=CoreStorage,
            CollectionChange=lambda *args: args,
            CHANGE_ADDED="added",
            CHANGE_UPDATED="updated",
            ItemNotFound=ItemNotFound,
        ),
    )
    collection = ImmediateStorage()
    hass = _hass(collection, "input_boolean")
    user = authorize(hass)
    monkeypatch.setattr(wsapi.er, "async_get", lambda _: FakeRegistry())
    backend = native.NativeBackend(hass)
    return collection, user, backend, Engine(backend)


@pytest.mark.parametrize("action", ["create", "update"])
async def test_core_original_api_generates_and_persists_the_item(direct, action):
    collection, _user, backend, _engine = direct
    arguments = {
        "helper_type": "input_boolean",
        "action": action,
        "data": {"name": "Changed"},
    }
    if action == "update":
        arguments.update(
            item_id="target", expected_item=copy.deepcopy(collection.data["target"])
        )
    result = await backend.execute(
        "helpers.storage.write", arguments, CallContext("actor")
    )
    assert result["success"]
    assert collection.public_calls == [action]
    assert collection.saves == 1 and len(collection.changes) == 1
    assert (
        result["item"] == collection.data["changed" if action == "create" else "target"]
    )


async def test_future_suspending_helper_validator_is_refused_before_core_dispatch(
    direct, monkeypatch
):
    collection, _user, backend, _engine = direct

    async def changed(self, current, fields):
        await asyncio.sleep(0)
        return {**current, **fields}

    monkeypatch.setattr(ImmediateStorage, "_update_data", changed)
    with pytest.raises(ToolFailure) as caught:
        await backend.execute(
            "helpers.storage.write",
            {
                "helper_type": "input_boolean",
                "action": "update",
                "item_id": "target",
                "data": {"name": "Changed"},
            },
            CallContext("actor"),
        )
    assert caught.value.status == "not_started"
    assert not collection.public_calls and collection.saves == 0


async def test_revocation_after_core_commit_never_becomes_a_prewrite_refusal(direct):
    collection, user, backend, engine = direct
    collection.notified.clear()
    actor = CallContext("actor")
    task = asyncio.create_task(
        engine.call_owned(
            "update",
            lambda: backend.execute(
                "helpers.storage.write",
                {
                    "helper_type": "input_boolean",
                    "action": "update",
                    "item_id": "target",
                    "data": {"name": "Changed"},
                },
                actor,
            ),
            actor,
            writes=True,
        )
    )
    await asyncio.wait_for(collection.notifying.wait(), 2)
    user.is_active = False
    collection.notified.set()
    result = await task
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["config_applied"] is True
    assert collection.public_calls == ["update"] and collection.saves == 1


async def test_future_core_await_between_validation_and_commit_is_refused(
    direct, monkeypatch
):
    collection, _user, backend, _engine = direct

    async def changed_api(self, item_id, fields):
        updated = await self._update_data(self.data[item_id], fields)
        await asyncio.sleep(0)
        self.data[item_id] = updated
        return updated

    monkeypatch.setattr(CoreStorage, "async_update_item", changed_api)
    with pytest.raises(ToolFailure) as caught:
        await backend.execute(
            "helpers.storage.write",
            {
                "helper_type": "input_boolean",
                "action": "update",
                "item_id": "target",
                "data": {"name": "Changed"},
            },
            CallContext("actor"),
        )
    assert caught.value.status == "not_started"
    assert collection.data["target"]["name"] == "Original"
    assert not collection.public_calls and collection.saves == 0


async def test_schema_rejection_remains_before_the_core_dispatch_boundary(
    direct, monkeypatch
):
    from voluptuous import Invalid

    collection, _user, backend, _engine = direct

    async def invalid(self, fields):
        raise Invalid("Rejected field")

    monkeypatch.setattr(ImmediateStorage, "_process_create_data", invalid)
    result = await backend.execute(
        "helpers.storage.write",
        {
            "helper_type": "input_boolean",
            "action": "create",
            "data": {"name": "Invalid"},
        },
        CallContext("actor"),
    )
    assert not result["success"] and result["error"]["code"] == "invalid"
    assert not collection.public_calls and collection.saves == 0
