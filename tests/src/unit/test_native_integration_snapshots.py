"""Integration writes use the existing native snapshot format and source whitelist."""

import copy
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext

from .test_native_snapshot_restore import runtime as runtime


@pytest.mark.parametrize("kind", ["integration", "flow"])
async def test_integration_setter_captures_only_the_supported_known_before_image(
    runtime, monkeypatch, kind
):
    _manager, snapshots, store, _path, _rows, _calls = runtime
    before = {
        "entry_id": "entry",
        "domain": "demo",
        "title": "Before",
        "disabled_by": None,
    }
    helper_type = "integration" if kind == "integration" else "utility_meter"
    domain = "integration" if kind == "integration" else "helper_utility_meter"
    if kind == "flow":
        before = {
            "entry_id": "entry",
            "options": {"source": "sensor.energy"},
            "entities": [],
        }
        monkeypatch.setattr(
            "custom_components.ha_mcp_tools.native_operations.snapshot_sources.flow_snapshot",
            AsyncMock(return_value=copy.deepcopy(before)),
        )
    payload = {
        "kind": kind,
        "helper_type": helper_type,
        "backup_domain": domain,
        "source_tool": "ha_set_integration",
        "backup_id": "entry",
        "entry_id": "entry",
        "current_entry": copy.deepcopy(before),
        "action": "update",
    }
    result = await snapshots.helper_backup(payload, CallContext("owner"))
    assert result is None or not result.get("warnings")
    records = store.list_snapshots(domain=domain)
    assert len(records) == 1
    saved = store.read_snapshot(records[0]["name"])
    assert saved["config"] == before
    assert saved["tool"] == "ha_set_integration"
