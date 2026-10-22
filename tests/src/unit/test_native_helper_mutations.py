"""Core helper validation may await; admission must remain true at commit."""

import asyncio
import copy
import json
import sys
from types import SimpleNamespace

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_component_helper_collections import FakeRegistry, ItemNotFound, _hass
from .test_component_ws_search import wsapi
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


class CoreStorage:
    def __init__(self):
        self.data = {"target": {"id": "target", "name": "Original"}}
        self.validating = asyncio.Event()
        self.validated = asyncio.Event()
        self.notifying = asyncio.Event()
        self.notified = asyncio.Event()
        self.notified.set()
        self.saves = 0
        self.changes = []
        self.id_manager = SimpleNamespace(generate_id=lambda name: name.lower())

    async def _process_create_data(self, fields):
        self.validating.set()
        await self.validated.wait()
        return dict(fields)

    async def _update_data(self, current, fields):
        self.validating.set()
        await self.validated.wait()
        return {**current, **fields}

    def _get_suggested_id(self, fields):
        return fields["name"]

    def _create_item(self, item_id, fields):
        return {"id": item_id, **fields}

    def _async_schedule_save(self):
        self.saves += 1

    def _serialize_item(self, item_id, item):
        return item

    def _hash_item(self, item):
        return json.dumps(item, sort_keys=True)

    async def notify_changes(self, changes):
        self.changes.extend(changes)
        self.notifying.set()
        await self.notified.wait()

    async def async_create_item(self, fields):
        validated = await self._process_create_data(fields)
        item_id = self.id_manager.generate_id(self._get_suggested_id(validated))
        item = self._create_item(item_id, validated)
        self.data[item_id] = item
        self._async_schedule_save()
        await self.notify_changes([("added", item_id, item)])
        return item

    async def async_update_item(self, item_id, fields):
        if item_id not in self.data:
            raise ItemNotFound(item_id)
        updated = await self._update_data(self.data[item_id], fields)
        self.data[item_id] = updated
        self._async_schedule_save()
        await self.notify_changes([("updated", item_id, updated)])
        return self.data[item_id]


@pytest.fixture
async def storage(native, monkeypatch):
    core = SimpleNamespace(
        StorageCollection=CoreStorage,
        DictStorageCollection=CoreStorage,
        CollectionChange=lambda *args: args,
        CHANGE_ADDED="added",
        CHANGE_UPDATED="updated",
        ItemNotFound=ItemNotFound,
    )
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.collection", core)
    collection = CoreStorage()
    hass = _hass(collection, "input_boolean")
    user = authorize(hass)
    monkeypatch.setattr(wsapi.er, "async_get", lambda hass: FakeRegistry())
    backend = native.NativeBackend(hass)
    return collection, user, backend, Engine(backend)


async def updating(storage):
    collection, _, backend, engine = storage
    context = CallContext("actor")
    expected = copy.deepcopy(collection.data["target"])
    task = asyncio.create_task(
        engine.call_owned(
            "helper-update",
            lambda: backend.execute(
                "helpers.storage.write",
                {
                    "helper_type": "input_boolean",
                    "action": "update",
                    "item_id": "target",
                    "data": {"name": "AI edit"},
                    "expected_item": expected,
                },
                context,
            ),
            context,
            writes=True,
        )
    )
    await collection.validating.wait()
    return task


@pytest.mark.parametrize("change", ["external_edit", "revoke", "read_only"])
async def test_validation_cannot_cross_a_changed_item_or_revoked_admission(
    storage, change
):
    collection, user, _, engine = storage
    task = await updating(storage)
    if change == "external_edit":
        collection.data["target"] = {"id": "target", "name": "Human edit"}
    elif change == "revoke":
        user.is_active = False
    else:
        engine.read_only = True
    before = copy.deepcopy(collection.data)
    collection.validated.set()
    result = await task
    assert result.is_error, result
    assert result.status == "not_started", result
    assert collection.data == before
    assert collection.saves == 0 and not collection.changes


async def test_revocation_after_commit_preserves_the_partial_outcome(storage):
    collection, user, _, _ = storage
    collection.notified.clear()
    task = await updating(storage)
    collection.validated.set()
    await collection.notifying.wait()
    assert collection.data["target"]["name"] == "AI edit"
    user.is_active = False
    collection.notified.set()
    result = await task
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["config_applied"] is True
    assert collection.saves == 1 and len(collection.changes) == 1


async def test_snapshot_compare_is_type_sensitive(storage):
    collection, _, backend, _ = storage
    collection.data["target"]["initial"] = False
    expected = {**collection.data["target"], "initial": 0}
    with pytest.raises(ToolFailure) as caught:
        await backend.execute(
            "helpers.storage.write",
            {
                "helper_type": "input_boolean",
                "action": "update",
                "item_id": "target",
                "data": {"name": "AI edit"},
                "expected_item": expected,
            },
            CallContext("actor"),
        )
    assert (
        caught.value.code == "CONFIG_CONFLICT" and caught.value.status == "not_started"
    )
    assert not collection.validating.is_set() and collection.saves == 0


async def test_create_rechecks_collection_and_admission_after_validation(storage):
    collection, _, backend, engine = storage
    actor = CallContext("actor")
    task = asyncio.create_task(
        engine.call_owned(
            "helper-create",
            lambda: backend.execute(
                "helpers.storage.write",
                {
                    "helper_type": "input_boolean",
                    "action": "create",
                    "data": {"name": "New"},
                },
                actor,
            ),
            actor,
            writes=True,
        )
    )
    await collection.validating.wait()
    collection.data["new"] = {"id": "new", "name": "Human create"}
    collection.validated.set()
    result = await task
    assert result.is_error and result.status == "not_started"
    assert collection.data["new"]["name"] == "Human create" and collection.saves == 0


async def test_successful_create_keeps_Core_ID_hash_and_listener_protocol(storage):
    collection, _, backend, _ = storage
    collection.validated.set()
    result = await backend.execute(
        "helpers.storage.write",
        {"helper_type": "input_boolean", "action": "create", "data": {"name": "New"}},
        CallContext("actor"),
    )
    assert result["success"] and result["item"]["id"] == "new"
    assert collection.saves == 1
    change, item_id, item, digest = collection.changes[0]
    assert (change, item_id, item, digest) == (
        "added",
        "new",
        collection.data["new"],
        collection._hash_item(item),
    )


async def test_idempotent_update_with_a_failed_listener_remains_an_unknown_outcome(
    storage,
):
    collection, _, backend, engine = storage
    collection.validated.set()
    listeners = []

    async def partially_applied(changes):
        listeners.append(changes)
        raise ValueError("listener failed after dispatch")

    collection.notify_changes = partially_applied
    actor = CallContext("actor")
    result = await engine.call_owned(
        "helper-update",
        lambda: backend.execute(
            "helpers.storage.write",
            {
                "helper_type": "input_boolean",
                "action": "update",
                "item_id": "target",
                "data": {"name": "Original"},
                "expected_item": dict(collection.data["target"]),
            },
            actor,
        ),
        actor,
        writes=True,
    )
    assert result.is_error and result.status == "outcome_unknown", result
    assert result.data["config_applied"] is True and collection.saves == 1
    assert len(listeners) == 1
