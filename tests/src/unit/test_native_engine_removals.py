"""Shared removal bodies capture before dispatch and never replay an issued delete."""

import asyncio
import copy
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.integration_removals import IntegrationRemovalTools
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_component_ws_search import FakeRegEntry, wsapi
from .test_native_catalog_contract_zones import _setup
from .test_native_engine_backend import native as native


def _removal_engine(native: Any, monkeypatch: pytest.MonkeyPatch, backup: Any) -> tuple:
    engine, collection, hass = _setup(native, monkeypatch, backup)
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.removals"
    )
    monkeypatch.setattr(module, "er", wsapi.er)
    validator = SimpleNamespace(entity_id=lambda value: value.lower())
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.config_validation", validator
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "config_validation",
        validator,
        raising=False,
    )
    engine.tools["ha_remove_helpers_integrations"] = define_tool(
        engine.backend,
        IntegrationRemovalTools,
        "ha_remove_helpers_integrations",
        annotations={"readOnlyHint": False},
    )
    return engine, collection, hass


async def test_native_storage_removal_captures_the_canonical_item_once(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = []

    async def backup(payload: dict, _actor: CallContext) -> dict:
        captured.append(copy.deepcopy(payload))
        assert collection.data["office"]["radius"] == 100
        return {"warnings": ["Backup disk warning"]}

    engine, collection, _ = _removal_engine(native, monkeypatch, backup)
    result = await engine.call(
        "ha_remove_helpers_integrations",
        {
            "target": "office",
            "helper_type": "zone",
            "confirm": True,
            "wait": False,
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert collection.data == {} and collection.deletes == 1
    assert result.data["entity_ids"] == ["zone.renamed"]
    assert result.data["warnings"] == ["Backup disk warning"]
    assert len(captured) == 1
    assert captured[0]["current_item"]["radius"] == 100
    assert captured[0]["source_tool"] == "ha_remove_helpers_integrations"


@pytest.mark.parametrize("change", ["typed_config", "read_only"])
async def test_native_storage_removal_rechecks_after_backup(
    native: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    async def backup(_payload: dict, _actor: CallContext) -> dict:
        if change == "typed_config":
            collection.data["office"]["passive"] = 0
        else:
            engine.read_only = True
        return {}

    engine, collection, _ = _removal_engine(native, monkeypatch, backup)
    result = await engine.call(
        "ha_remove_helpers_integrations",
        {
            "target": "office",
            "helper_type": "zone",
            "confirm": True,
            "wait": False,
        },
        CallContext("actor"),
    )
    assert result.is_error
    assert result.data["error"]["code"] == (
        "CONFIG_CONFLICT" if change == "typed_config" else "READ_ONLY"
    )
    assert collection.deletes == 0


async def test_native_storage_remove_then_raise_is_unknown_without_replay(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, collection, _ = _removal_engine(
        native, monkeypatch, AsyncMock(return_value={})
    )
    collection.fail_after_delete = True
    result = await engine.call(
        "ha_remove_helpers_integrations",
        {
            "target": "office",
            "helper_type": "zone",
            "confirm": True,
            "wait": False,
        },
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert collection.deletes == 1 and collection.data == {}


async def test_native_storage_remove_drains_double_cancellation(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, collection, _ = _removal_engine(
        native, monkeypatch, AsyncMock(return_value={})
    )
    started, finish = asyncio.Event(), asyncio.Event()

    async def remove(item_id: str) -> None:
        collection.deletes += 1
        collection.data.pop(item_id)
        started.set()
        await finish.wait()

    collection.async_delete_item = remove
    task = asyncio.create_task(
        engine.backend.execute(
            "helpers.storage.remove",
            {
                "helper_type": "zone",
                "item_id": "office",
                "expected_item": copy.deepcopy(collection.data["office"]),
            },
            CallContext("actor"),
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        _ = await task
    assert collection.deletes == 1 and collection.data == {}


async def test_native_entry_removal_rechecks_revision_and_uses_core_owner(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnknownEntry(Exception):
        pass

    class UnknownSubEntry(Exception):
        pass

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.config_entries",
        SimpleNamespace(UnknownEntry=UnknownEntry, UnknownSubEntry=UnknownSubEntry),
    )
    current = {"entry_id": "entry", "domain": "hue", "modified_at": 1}
    entry = SimpleNamespace(domain="hue", as_json_fragment=current)
    engine, _, hass = _removal_engine(native, monkeypatch, AsyncMock(return_value={}))
    remove = AsyncMock(return_value={"require_restart": True})
    hass.config_entries = SimpleNamespace(
        async_get_entry=lambda _key: entry, async_remove=remove
    )
    old = copy.deepcopy(current)
    current["modified_at"] = 2
    with pytest.raises(ToolFailure, match="changed after its removal snapshot"):
        await engine.backend.execute(
            "config.entries.remove",
            {"entry_id": "entry", "expected_entry": old},
            CallContext("actor"),
        )
    remove.assert_not_awaited()
    result = await engine.backend.execute(
        "config.entries.remove",
        {"entry_id": "entry", "expected_entry": copy.deepcopy(current)},
        CallContext("actor"),
    )
    assert result == {"require_restart": True}
    remove.assert_awaited_once_with("entry")


@pytest.mark.parametrize("mode", ["direct", "flow", "subentry"])
async def test_shared_native_entry_removal_routes_capture_and_uses_core_once(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    class UnknownEntry(Exception):
        pass

    class UnknownSubEntry(Exception):
        pass

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.config_entries",
        SimpleNamespace(UnknownEntry=UnknownEntry, UnknownSubEntry=UnknownSubEntry),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.loader",
        SimpleNamespace(
            async_get_config_flows=AsyncMock(return_value={"utility_meter"})
        ),
    )
    captured = []

    async def backup(payload: dict, _actor: CallContext) -> dict:
        captured.append(copy.deepcopy(payload))
        return {}

    engine, _, hass = _removal_engine(native, monkeypatch, backup)
    metadata = {
        "entry_id": "entry",
        "domain": "utility_meter" if mode == "flow" else "hue",
        "modified_at": 1,
    }
    entry = SimpleNamespace(
        domain=metadata["domain"],
        as_json_fragment=metadata,
        subentries={
            "child": SimpleNamespace(
                subentry_id="child",
                subentry_type="sensor",
                title="Sensor",
                unique_id="uid",
            ),
        },
    )
    deleted = []

    async def remove(entry_id: str) -> dict:
        deleted.append(entry_id)
        return {"require_restart": True}

    def remove_subentry(actual: Any, subentry_id: str) -> bool:
        assert actual is entry
        deleted.append(subentry_id)
        actual.subentries.pop(subentry_id)
        return True

    hass.config_entries = SimpleNamespace(
        async_get_entry=lambda key: entry if key == "entry" else None,
        async_remove=remove,
        async_remove_subentry=remove_subentry,
    )
    registry = wsapi.er.async_get(hass)
    row = FakeRegEntry(
        "sensor.meter",
        platform="utility_meter",
        config_entry_id="entry",
        unique_id="meter",
    )
    row.extended_dict = row.as_partial_dict
    registry.entries[row.entity_id] = row
    monkeypatch.setattr(
        wsapi.er,
        "async_entries_for_config_entry",
        lambda reg, key: [row] if key == "entry" else [],
        raising=False,
    )
    arguments = {
        "target": "sensor.meter" if mode == "flow" else "entry",
        "confirm": True,
        "wait": False,
    }
    if mode == "flow":
        arguments["helper_type"] = "utility_meter"
    elif mode == "subentry":
        arguments.update(helper_type="config_subentry", subentry_id="child")
    result = await engine.call(
        "ha_remove_helpers_integrations", arguments, CallContext("actor")
    )
    assert not result.is_error, result.data
    assert deleted == ["child" if mode == "subentry" else "entry"]
    assert len(captured) == 1
    assert captured[0]["kind"] == ("integration" if mode == "direct" else mode)
    assert captured[0]["entry_id"] == "entry"
    if mode == "subentry":
        assert entry.subentries == {}
    else:
        assert result.data["require_restart"] is True
