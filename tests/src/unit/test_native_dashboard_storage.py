"""Lovelace calls retain Core owners and compare after every preparation await."""

import asyncio
import copy
import importlib
import json
import sys
from types import SimpleNamespace

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native


class ConfigNotFound(Exception):
    pass


class LovelaceStorage:
    mode = "storage"

    def __init__(self, metadata=None, body=None):
        self.config = metadata
        self.url_path = metadata["url_path"] if metadata else None
        self._data = {"config": copy.deepcopy(body)}
        self.calls = []
        self.loaded = asyncio.Event()
        self.release = None

    async def _load(self):
        self._data = {"config": None}
        return self._data

    async def async_load(self, force):
        if self._data is None:
            await self._load()
        if self._data["config"] is None:
            raise ConfigNotFound
        return self._data["config"]

    async def async_save(self, config):
        if self._data is None:
            await self._load()
        self._data["config"] = config
        self.calls.append(copy.deepcopy(config))
        self.loaded.set()
        if self.release is not None:
            await self.release.wait()

    async def async_delete(self):
        self._data = None
        self.calls.append(None)


class StorageCollection:
    async def async_create_item(self, data):
        data = await self._process_create_data(data)
        identifier = data["url_path"].replace("-", "_")
        self.data[identifier] = {"id": identifier, **data}
        self.dashboards[data["url_path"]] = LovelaceStorage(self.data[identifier])
        return self.data[identifier]

    async def async_update_item(self, identifier, fields):
        values = await self._update_data(self.data[identifier], fields)
        self.data[identifier] = values
        self.dashboards[values["url_path"]].config = values
        await self.notify()
        return values

    async def async_delete_item(self, identifier):
        row = self.data.pop(identifier)
        self.dashboards.pop(row["url_path"])


class DashboardsCollection(StorageCollection):
    def __init__(self, dashboards):
        self.dashboards = dashboards
        self.data = {
            owner.config["id"]: owner.config
            for owner in dashboards.values()
            if owner.config
        }

    async def _process_create_data(self, fields):
        return fields

    async def _update_data(self, current, fields):
        return {**current, **fields}

    async def notify(self):
        pass


@pytest.fixture
def dashboards(native, monkeypatch):
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.dashboard_storage"
    )
    access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.registry_access"
    )
    monkeypatch.setattr(access, "json_bytes", lambda value: json.dumps(value).encode())
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.lovelace.dashboard",
        SimpleNamespace(
            LovelaceStorage=LovelaceStorage, DashboardsCollection=DashboardsCollection
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.lovelace.const",
        SimpleNamespace(ConfigNotFound=ConfigNotFound),
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.collection"],
        "StorageCollection",
        StorageCollection,
    )
    owner = LovelaceStorage(
        {
            "id": "my_dash",
            "url_path": "my-dash",
            "title": "Before",
            "future_field": False,
        },
        {"views": []},
    )
    owners = {"my-dash": owner, None: LovelaceStorage()}
    collection = DashboardsCollection(owners)
    monkeypatch.setattr(
        module,
        "collection_owner",
        lambda *_: SimpleNamespace(storage_collection=collection),
    )
    hass = SimpleNamespace(
        config=SimpleNamespace(recovery_mode=False),
        data={
            "lovelace": SimpleNamespace(dashboards=owners),
            "websocket_api": {
                f"lovelace/dashboards/{action}": (None, dict)
                for action in ("create", "update", "delete")
            },
        },
    )
    active = [True]

    async def authorize(actor):
        if not active[0]:
            raise PermissionError("revoked")

    storage = module.DashboardStorage(hass, CallContext("owner"), authorize=authorize)
    return storage, owner, collection, active, module


async def test_dashboard_storage_uses_core_metadata_and_body_apis(dashboards):
    storage, owner, collection, _active, _module = dashboards
    before = await storage.read("my_dash")
    assert before.url_path == "my-dash"
    updated = await storage.update_metadata(before, {"title": "Changed"})
    assert updated.row["future_field"] is False
    saved = await storage.save(updated, {"views": [{"title": "Main"}]})
    assert owner.calls == [{"views": [{"title": "Main"}]}]
    assert saved.config == owner.calls[0]
    await storage.remove(saved)
    assert not collection.data and storage.resolve("my-dash")[1] is None


async def test_dashboard_create_then_first_save_uses_preloaded_empty_owner(dashboards):
    storage, _owner, collection, *_ = dashboards
    before = await storage.read("new-dash")
    created = await storage.create(before, {"title": "New"})
    assert created.config is None and created.row["id"] == "new_dash"
    result = await storage.save(created, {"views": []})
    assert result.config == {"views": []} and "new_dash" in collection.data


@pytest.mark.parametrize("change", ["revoke", "body", "replace"])
async def test_dashboard_save_rechecks_inside_owned_task(
    dashboards, monkeypatch, change
):
    storage, owner, _collection, active, module = dashboards
    before = await storage.read("my-dash")
    owned = module.await_owned_io

    async def changed(awaitable):
        if change == "revoke":
            active[0] = False
        elif change == "body":
            owner._data["config"] = {"views": [{"title": "Concurrent"}]}
        else:
            storage.hass.data["lovelace"].dashboards["my-dash"] = LovelaceStorage(
                copy.deepcopy(owner.config), before.config
            )
        return await owned(awaitable)

    monkeypatch.setattr(module, "await_owned_io", changed)
    with pytest.raises(ToolFailure) as error:
        await storage.save(before, {"views": [{"title": "Desired"}]})
    assert error.value.status == "not_started" and not owner.calls


async def test_dashboard_collection_change_before_listener_is_not_overwritten(
    dashboards,
):
    storage, owner, collection, *_ = dashboards
    before = await storage.read("my-dash")
    collection.data["my_dash"] = {**collection.data["my_dash"], "future_field": 0}
    with pytest.raises(ToolFailure) as error:
        await storage.update_metadata(before, {"title": "Should not write"})
    assert error.value.status == "not_started"
    assert (
        owner.config["title"] == "Before"
        and collection.data["my_dash"]["future_field"] == 0
    )


async def test_dashboard_save_cancellation_drains_core_persistence(dashboards):
    storage, owner, *_ = dashboards
    owner.release = asyncio.Event()
    before = await storage.read("my-dash")
    task = asyncio.create_task(storage.save(before, {"views": [{"title": "Saved"}]}))
    await owner.loaded.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    owner.release.set()
    with pytest.raises(asyncio.CancelledError):
        _ = await task
    assert len(owner.calls) == 1 and storage.completed


async def test_dashboard_future_unconditional_load_is_refused_before_dispatch(
    dashboards, monkeypatch
):
    storage, owner, collection, *_ = dashboards

    class FutureStorage(LovelaceStorage):
        async def async_save(self, config):
            await self._load()
            self._data["config"] = config
            self.calls.append(config)

    replacement = FutureStorage(copy.deepcopy(owner.config), owner._data["config"])
    collection.dashboards["my-dash"] = replacement
    monkeypatch.setattr(
        sys.modules["homeassistant.components.lovelace.dashboard"],
        "LovelaceStorage",
        FutureStorage,
    )
    before = await storage.read("my-dash")
    with pytest.raises(ToolFailure) as error:
        await storage.save(before, {"views": [{"title": "Should not dispatch"}]})
    assert error.value.status == "not_started" and not replacement.calls


async def test_dashboard_guard_targets_the_config_assignment_not_an_earlier_store(
    dashboards, monkeypatch
):
    storage, owner, collection, *_ = dashboards

    class FutureStorage(LovelaceStorage):
        async def async_save(self, config):
            self.audit["note"] = "before yield"
            await asyncio.sleep(0)
            self._data["config"] = config
            self.calls.append(config)

    replacement = FutureStorage(copy.deepcopy(owner.config), owner._data["config"])
    replacement.audit = {}
    collection.dashboards["my-dash"] = replacement
    monkeypatch.setattr(
        sys.modules["homeassistant.components.lovelace.dashboard"],
        "LovelaceStorage",
        FutureStorage,
    )
    before = await storage.read("my-dash")
    with pytest.raises(ToolFailure) as error:
        await storage.save(before, {"views": [{"title": "Should not dispatch"}]})
    assert (
        error.value.status == "not_started"
        and not replacement.calls
        and replacement.audit == {}
    )
