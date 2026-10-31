"""Registry identity and the user settings restored by entity-registry history."""

from typing import Any

from ha_mcp.runtime.snapshot_payloads import InvalidBackupSnapshotError
from ha_mcp.utils.config_hash import json_values_equal

_IDENTITY_FIELDS = ("id", "created_at", "platform", "unique_id", "config_entry_id")
_USER_FIELDS = (
    "area_id",
    "device_class",
    "disabled_by",
    "hidden_by",
    "icon",
    "name",
    "aliases",
    "labels",
)


def entity_registry_identity(row: dict[str, Any]) -> dict[str, Any]:
    if not row.get("id") or not row.get("entity_id") or row.get("created_at") is None:
        raise InvalidBackupSnapshotError(
            "Entity registry snapshot has no durable identity"
        )
    return {key: row.get(key) for key in _IDENTITY_FIELDS}


def entity_restore_payloads(
    saved: dict[str, Any],
    current: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build Core update requests; Core retains all validation and conversions.

    The snapshot preserves every Core field. Restoration changes only the user
    settings exposed by config/entity_registry/update; runtime/device and
    integration-owned fields are not synthetic state-machine writes.
    """
    entity_registry_identity(saved)
    entity_registry_identity(current)
    base = {
        "entity_id": current["entity_id"],
        **{
            key: saved.get(key)
            for key in _USER_FIELDS
            if not json_values_equal(saved.get(key), current.get(key))
        },
    }
    if saved["entity_id"] != current["entity_id"]:
        base["new_entity_id"] = saved["entity_id"]
    categories = {
        scope: saved.get("categories", {}).get(scope)
        for scope in current.get("categories", {}) | saved.get("categories", {})
    }
    if not json_values_equal(
        saved.get("categories", {}), current.get("categories", {})
    ):
        base["categories"] = categories
    options = saved.get("options", {})
    return [
        *([base] if len(base) > 1 else []),
        *[
            {
                "entity_id": saved["entity_id"],
                "options_domain": domain,
                "options": options.get(domain),
            }
            for domain in current.get("options", {}) | options
            if not json_values_equal(
                current.get("options", {}).get(domain), options.get(domain)
            )
        ],
    ]
