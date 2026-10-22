"""Flow snapshots keep scrubbed, resolved identities and the legacy restore shape."""

import copy
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.snapshot_payloads import _FlowHelperReadError

from .test_native_engine_backend import native as native


@pytest.fixture
def source(native, monkeypatch):
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.snapshot_sources"
    )
    overview = importlib.import_module(
        "custom_components.ha_mcp_tools.websocket_api.overview"
    )
    entries = importlib.import_module(
        "custom_components.ha_mcp_tools.websocket_api.config_entries"
    )
    monkeypatch.setattr(overview, "_helpers_list_prep", AsyncMock(return_value={}))
    monkeypatch.setattr(entries, "_config_entries_prep", AsyncMock(return_value={}))
    registry = SimpleNamespace(
        async_get=lambda hass: "registry",
        async_entries_for_config_entry=lambda registry, entry_id: [
            SimpleNamespace(
                entity_id="sensor.renamed",
                unique_id="stable-id",
                name="Renamed",
                original_name="Original",
            )
        ],
    )
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", registry)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "entity_registry", registry
    )
    return module, overview, entries


def flow_result():
    return {
        "covered_types": ["utility_meter"],
        "helpers": [
            {
                "kind": "flow",
                "helper_type": "utility_meter",
                "entry_id": "resolved",
                "options": {"source": "sensor.energy", "cycle": "daily"},
            }
        ],
    }


async def test_flow_capture_uses_scrubbed_resolved_record_and_registry(
    source, monkeypatch
):
    module, overview, _ = source
    monkeypatch.setattr(overview, "_do_helpers_list", lambda *a, **kw: flow_result())
    snapshot = await module.flow_snapshot(
        object(),
        {
            "helper_type": "utility_meter",
            "entry_id": "resolved",
            "current_item": {"credentials": "never use caller data"},
        },
    )
    assert snapshot == {
        "entry_id": "resolved",
        "options": {"source": "sensor.energy", "cycle": "daily"},
        "entities": [
            {
                "entity_id": "sensor.renamed",
                "unique_id": "stable-id",
                "name": "Renamed",
                "original_name": "Original",
            }
        ],
    }


@pytest.mark.parametrize(
    "change,reason",
    [
        ("degraded", "secret_scrub_degraded"),
        ("uncovered", "uncovered_type"),
        ("duplicate", "ambiguous_entry_identity"),
        ("missing", "entry_missing"),
        ("redacted", "redacted_options"),
        ("withheld", "options_withheld"),
    ],
)
async def test_flow_capture_refuses_incomplete_restore_values(
    source, monkeypatch, change, reason
):
    module, overview, _ = source
    result = flow_result()
    if change == "degraded":
        result["secret_scrub_degraded"] = True
    elif change == "uncovered":
        result["covered_types"] = []
    elif change == "duplicate":
        result["helpers"].append(copy.deepcopy(result["helpers"][0]))
    elif change == "missing":
        result["helpers"] = []
    elif change == "redacted":
        result["helpers"][0]["options"]["source"] = "**redacted**"
    else:
        result["helpers"][0]["options_withheld"] = True
    monkeypatch.setattr(overview, "_do_helpers_list", lambda *a, **kw: result)
    with pytest.raises(_FlowHelperReadError) as caught:
        await module.flow_snapshot(
            object(), {"helper_type": "utility_meter", "entry_id": "resolved"}
        )
    assert caught.value.reason == reason


@pytest.mark.parametrize(
    "change", ["valid", "redacted", "duplicate", "type_changed", "missing_parent"]
)
async def test_subentry_capture_keeps_original_restore_shape_and_checks_identity(
    source, monkeypatch, change
):
    module, _, entries = source
    row = {
        "subentry_id": "child",
        "subentry_type": "sensor",
        "title": "Real title",
        "data": {"template": "{{ 42 }}"},
    }
    result = {"entries": [{"entry_id": "resolved", "subentries": [row]}]}
    if change == "redacted":
        row["data"]["template"] = "**redacted**"
    elif change == "duplicate":
        result["entries"][0]["subentries"].append(copy.deepcopy(row))
    elif change == "missing_parent":
        result["entries"] = []
    monkeypatch.setattr(entries, "_do_config_entries", lambda *a, **kw: result)
    payload = {
        "entry_id": "resolved",
        "subentry_id": "child",
        "subentry_type": "other" if change == "type_changed" else "sensor",
        "title": "untrusted title",
    }
    if change != "valid":
        with pytest.raises(_FlowHelperReadError):
            await module.subentry_snapshot(object(), payload)
        return
    assert await module.subentry_snapshot(object(), payload) == {
        "entry_id": "resolved",
        **row,
    }
