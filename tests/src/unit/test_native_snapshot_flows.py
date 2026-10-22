"""Native restore drives the real shared walker and owned Core flow facade."""

import copy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native
from .test_native_engine_flows import flows as flows
from .test_native_snapshot_restore import runtime as runtime


def prepare(runtime, native, monkeypatch):
    from custom_components.ha_mcp_tools import (
        native_snapshot_domains,
        native_snapshot_flows,
    )

    manager, snapshots, store, _path, _rows, _calls = runtime
    before = {
        "entry_id": "entry",
        "options": {"template_type": "sensor", "name": "Current", "state": "0"},
        "entities": [],
    }
    expected = {
        "entry_id": "entry",
        "options": {"template_type": "sensor", "name": "Restored", "state": "1"},
        "entities": [],
    }
    current = copy.deepcopy(before)
    entry = SimpleNamespace(
        entry_id="entry",
        domain="template",
        modified_at=datetime(2026, 10, 20, tzinfo=UTC),
    )
    entry.as_json_fragment = {
        "entry_id": "entry",
        "domain": "template",
        "modified_at": entry.modified_at.timestamp(),
    }
    core = SimpleNamespace(
        async_init=AsyncMock(
            return_value={
                "flow_id": "restore",
                "type": "form",
                "last_step": True,
                "step_id": "user",
                "data_schema": [{"name": "name"}, {"name": "state"}],
            }
        ),
        async_configure=AsyncMock(),
        async_abort=Mock(),
    )

    async def configure(_flow_id, data):
        current["options"].update(data)
        return {"flow_id": "restore", "type": "create_entry", "result": {}}

    core.async_configure.side_effect = configure
    manager.hass.config_entries = SimpleNamespace(
        options=core,
        flow=core,
        subentries=core,
        async_get_entry=lambda _id: entry,
        async_entries=lambda _domain=None: [entry],
    )
    manager.hass.auth = SimpleNamespace(
        async_get_user=AsyncMock(
            return_value=SimpleNamespace(
                id="owner", is_admin=True, is_active=True, system_generated=False
            )
        )
    )
    manager.backend = native.NativeBackend(manager.hass)
    manager.engine = Engine(manager.backend)
    manager.engine.enabled_tools = set()
    manager.policy.flags["enable_auto_backup"] = False

    async def read(_hass, _args):
        return copy.deepcopy(current)

    monkeypatch.setattr(native_snapshot_domains, "flow_snapshot", read)
    monkeypatch.setattr(native_snapshot_flows, "flow_snapshot", read)
    path = store.capture("helper_template", "entry", expected, "fixture")
    return manager, snapshots, store, path, core, current, before, expected


async def restore(manager, snapshots, path):
    actor = CallContext("owner")
    return await manager.engine.call_owned(
        "ha_manage_backup",
        lambda: snapshots.restore_snapshot(path.name, actor),
        actor,
        writes=True,
        enforce_catalogue=False,
        arguments={"scope": "edits", "action": "restore", "name": path.name},
    )


async def test_complete_native_options_restore_forces_safety_and_checks_readback(
    runtime, native, flows, monkeypatch
):
    manager, snapshots, store, path, core, current, before, expected = prepare(
        runtime, native, monkeypatch
    )
    result = await restore(manager, snapshots, path)
    assert not result.is_error, result.data
    assert current == expected
    assert result.data["verification_status"] == "matched"
    assert store.read_snapshot(result.data["safety_backup"])["config"] == before
    core.async_configure.assert_awaited_once_with(
        "restore", {"name": "Restored", "state": "1"}
    )
    assert not manager.backend._flows.owned


async def test_backup_read_only_change_blocks_next_flow_step_and_cleans_owned_flow(
    runtime, native, flows, monkeypatch
):
    manager, snapshots, _store, path, core, current, before, _expected = prepare(
        runtime, native, monkeypatch
    )
    initial = core.async_init.return_value

    async def start(*_args, **_kwargs):
        manager.policy.flags["backup_read_only"] = True
        return initial

    core.async_init.side_effect = start
    result = await restore(manager, snapshots, path)
    assert result.is_error
    assert current == before
    core.async_configure.assert_not_awaited()
    assert not manager.backend._flows.owned
