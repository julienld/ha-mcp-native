"""Native flow recreation preserves saved entity IDs through Core's registry."""

import copy
import sys
from types import SimpleNamespace

import pytest

from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native
from .test_native_engine_flows import flows as flows
from .test_native_snapshot_flows import prepare
from .test_native_snapshot_restore import runtime as runtime


async def test_removed_helper_recreates_through_core_then_restores_owned_entity_identity(
    runtime, native, flows, monkeypatch
):
    from custom_components.ha_mcp_tools import (
        native_snapshot_domains,
        native_snapshot_flows,
    )
    from ha_mcp.runtime.snapshot_payloads import _FlowHelperReadError

    manager, snapshots, store, _path, core, _current, _before, _expected = prepare(
        runtime, native, monkeypatch
    )
    entries, entities = {}, {}
    options = {"name": "Recovered meter", "source": "sensor.energy"}
    saved = {
        "entry_id": "old-entry",
        "options": options,
        "entities": [
            {
                "entity_id": "sensor.saved",
                "unique_id": "old-entry-energy",
                "name": "Saved name",
                "original_name": "Recovered meter",
            }
        ],
    }
    manager.hass.config_entries.async_get_entry = entries.get
    manager.hass.config_entries.async_entries = lambda *args: list(entries.values())
    manager.hass.states = SimpleNamespace(get=lambda _id: None)
    core.async_init.return_value = {
        "flow_id": "create",
        "type": "form",
        "step_id": "user",
        "last_step": True,
        "data_schema": [{"name": "name"}, {"name": "source"}],
    }

    async def configure(_flow, data):
        assert data == options
        entries["new-entry"] = SimpleNamespace(entry_id="new-entry")
        entities["sensor.generated"] = SimpleNamespace(
            entity_id="sensor.generated",
            unique_id="new-entry-energy",
            config_entry_id="new-entry",
            name=None,
            original_name="Recovered meter",
            id="registry-id",
        )
        return {
            "flow_id": "create",
            "type": "create_entry",
            "result": {"entry_id": "new-entry", "title": "Recovered meter"},
        }

    core.async_configure.side_effect = configure

    def update(source, **fields):
        row = entities.pop(source)
        row.entity_id = fields.get("new_entity_id", source)
        if "name" in fields:
            row.name = fields["name"]
        entities[row.entity_id] = row

    registry = SimpleNamespace(
        entities=entities, async_get=entities.get, async_update_entity=update
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.entity_registry"],
        "async_get",
        lambda _: registry,
    )

    async def snapshot(_hass, args):
        if args["entry_id"] not in entries:
            raise _FlowHelperReadError("Missing entry", "entry_missing")
        return {
            "entry_id": args["entry_id"],
            "options": copy.deepcopy(options),
            "entities": [],
        }

    monkeypatch.setattr(native_snapshot_domains, "flow_snapshot", snapshot)
    monkeypatch.setattr(native_snapshot_flows, "flow_snapshot", snapshot)
    path = store.capture("helper_utility_meter", "old-entry", saved, "fixture")
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["restore_mode"] == "recreated"
    assert result["entity_id"] == "new-entry"
    assert result["result"]["entity_id_mapping"] == [
        {"created_entity_id": "sensor.generated", "restored_entity_id": "sensor.saved"}
    ]
    assert set(entities) == {"sensor.saved"}
    assert entities["sensor.saved"].name == "Saved name"
    assert not manager.backend._flows.owned


@pytest.mark.parametrize("missing", [False, True])
async def test_subentry_restore_uses_owned_core_flow_and_verifies_title_and_data(
    runtime, native, flows, monkeypatch, missing
):
    from custom_components.ha_mcp_tools import (
        native_snapshot_domains,
        native_snapshot_flows,
    )

    manager, snapshots, store, _path, core, _current, _before, _expected = prepare(
        runtime, native, monkeypatch
    )
    entry = manager.hass.config_entries.async_get_entry("entry")
    previous = {"value": "old"}
    rows = (
        {}
        if missing
        else {
            "sub": SimpleNamespace(
                subentry_id="sub",
                subentry_type="sensor",
                title="Current",
                data=previous,
                unique_id="uid",
            )
        }
    )
    entry.subentries = rows
    saved = {
        "entry_id": "entry",
        "subentry_id": "sub",
        "subentry_type": "sensor",
        "title": "Saved",
        "data": {"value": "restored"},
    }
    core.async_init.return_value = {
        "flow_id": "subflow",
        "type": "form",
        "step_id": "user",
        "last_step": True,
        "data_schema": [{"name": "name"}, {"name": "value"}],
    }

    async def configure(_flow, data):
        new_id = "new-sub" if missing else "sub"
        rows[new_id] = SimpleNamespace(
            subentry_id=new_id,
            subentry_type="sensor",
            title=data["name"],
            data={"value": data["value"]},
            unique_id="uid",
        )
        if missing:
            return {"flow_id": "subflow", "type": "create_entry", "result": {}}
        return {
            "flow_id": "subflow",
            "type": "abort",
            "reason": "reconfigure_successful",
        }

    core.async_configure.side_effect = configure

    def record(row):
        return {
            "entry_id": "entry",
            "subentry_id": row.subentry_id,
            "subentry_type": row.subentry_type,
            "title": row.title,
            "data": copy.deepcopy(row.data),
        }

    async def all_rows(_hass, _entry):
        return [record(row) for row in rows.values()]

    async def snapshot(_hass, args):
        row = rows.get(args["subentry_id"])
        return record(row) if row is not None else None

    monkeypatch.setattr(native_snapshot_domains, "optional_subentry_snapshot", snapshot)
    monkeypatch.setattr(native_snapshot_flows, "optional_subentry_snapshot", snapshot)
    monkeypatch.setattr(native_snapshot_flows, "subentry_snapshots", all_rows)
    path = store.capture("helper_config_subentry", "entry/sub", saved, "fixture")
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["verification_status"] == "matched"
    restored = rows["new-sub" if missing else "sub"]
    assert restored.data == saved["data"] and restored.title == "Saved"
    if missing:
        assert result["restore_mode"] == "recreated"
        assert result["entity_id"] == "entry/new-sub"
    else:
        assert (
            store.read_snapshot(result["safety_backup"])["config"]["data"] == previous
        )
    assert not manager.backend._flows.owned
