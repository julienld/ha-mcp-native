"""Read restorable helper values from Core's existing scrubbed producers."""

from __future__ import annotations

import json
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.snapshot_payloads import (
    _flow_options,
    _FlowHelperBackupSkip,
    _FlowHelperReadError,
    _subentry_row,
)


async def flow_snapshot(hass: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from homeassistant.helpers import entity_registry as er

    from ..websocket_api.overview import _do_helpers_list, _helpers_list_prep

    helper_type, entry_id = payload["helper_type"], payload["entry_id"]
    params = {"helper_types": [helper_type], "include_flow_helpers": True}
    result = _do_helpers_list(hass, params, **await _helpers_list_prep(hass, params))
    _require_scrub(result)
    if helper_type not in result["covered_types"]:
        raise _FlowHelperReadError("Helper type cannot be enumerated", "uncovered_type")
    records = [
        row
        for row in result["helpers"]
        if row.get("kind") == "flow" and row.get("helper_type") == helper_type
    ]
    identities = [row.get("entry_id") for row in records]
    if any(not isinstance(value, str) or not value for value in identities) or len(
        set(identities)
    ) != len(identities):
        raise _FlowHelperReadError(
            "Helper listing has ambiguous identities", "ambiguous_entry_identity"
        )
    record = next((row for row in records if row["entry_id"] == entry_id), None)
    if record is None:
        raise _FlowHelperReadError("Resolved helper no longer exists", "entry_missing")
    if record.get("options_withheld"):
        raise _FlowHelperBackupSkip("Helper options are withheld", "options_withheld")
    snapshot = {
        "entry_id": entry_id,
        "options": _flow_options(record.get("options"), helper_type),
        "entities": [
            {
                "entity_id": row.entity_id,
                "unique_id": row.unique_id,
                "name": row.name,
                "original_name": row.original_name,
            }
            for row in er.async_entries_for_config_entry(er.async_get(hass), entry_id)
        ],
    }
    # Match the former Core JSON boundary before YAML capture and typed restore
    # comparisons: e.g. template_type is a Core StrEnum, not a persisted enum.
    serialized: dict[str, Any] = json.loads(json_bytes(snapshot))
    return serialized


async def subentry_snapshots(hass: Any, entry_id: str) -> list[dict[str, Any]]:
    """Read complete restorable subentry data; absence of the parent is an error."""
    from ..core_config_entries import config_entries

    result = await config_entries(hass, entry_id=entry_id, include_subentry_data=True)
    _require_scrub(result)
    entries = result["entries"]
    if len(entries) != 1 or entries[0]["entry_id"] != entry_id:
        raise _FlowHelperReadError(
            "Resolved parent entry no longer exists", "parent_entry_missing"
        )
    subentries = entries[0].get("subentries", [])
    if not isinstance(subentries, list):
        raise _FlowHelperReadError(
            "Subentry listing is incomplete", "subentry_identity"
        )
    ids = [row.get("subentry_id") for row in subentries if isinstance(row, dict)]
    if (
        len(ids) != len(subentries)
        or any(not isinstance(value, str) or not value for value in ids)
        or len(set(ids)) != len(ids)
    ):
        raise _FlowHelperReadError(
            "Subentry identities are ambiguous", "subentry_identity"
        )
    serialized: list[dict[str, Any]] = json.loads(json_bytes(subentries))
    return serialized


async def optional_subentry_snapshot(
    hass: Any, payload: dict[str, Any]
) -> dict[str, Any] | None:
    entry_id, subentry_id = payload["entry_id"], payload["subentry_id"]
    subentries = await subentry_snapshots(hass, entry_id)
    matches = [row for row in subentries if row.get("subentry_id") == subentry_id]
    if not matches:
        return None
    expected = payload.get("subentry_type")
    if expected is not None and matches[0].get("subentry_type") != expected:
        raise _FlowHelperReadError(
            "Subentry type changed before capture", "target_mismatch"
        )
    snapshot = _subentry_row(matches, entry_id, subentry_id)
    assert snapshot is not None
    return snapshot


async def subentry_snapshot(hass: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """A pre-write capture requires the already resolved subentry to remain present."""
    snapshot = await optional_subentry_snapshot(hass, payload)
    if snapshot is None:
        raise _FlowHelperReadError(
            "Subentry identity is missing or ambiguous", "subentry_identity"
        )
    return snapshot


def _require_scrub(result: dict[str, Any]) -> None:
    if result.get("secret_scrub_degraded"):
        raise _FlowHelperReadError(
            "Secret scrub is degraded; capture is unsafe", "secret_scrub_degraded"
        )
