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
    if any(
        not isinstance(row.get(key), str) or not row[key] for key in ("id", "entity_id")
    ):
        raise InvalidBackupSnapshotError(
            "Entity registry snapshot has no durable identity"
        )
    return {key: row[key] for key in _IDENTITY_FIELDS if key in row}


def entity_registry_matches(saved: dict[str, Any], current: dict[str, Any]) -> bool:
    """Older upstream images carry only the UUID; never invent newer identity fields."""
    identity = entity_registry_identity(saved)
    actual = entity_registry_identity(current)
    return all(
        key in actual and json_values_equal(actual[key], value)
        for key, value in identity.items()
    )


def entity_snapshot_target(data: dict[str, Any]) -> dict[str, Any]:
    """Select the historical payload's owner before live reads or safety capture.

    ``entity`` was used for both StateMachine values and registry settings.
    The persisted alias remains unchanged; registry operations resolve the UUID.
    """
    if data.get("domain") not in {"entity", "entity_registry"}:
        return data
    config, target = data["config"], data["entity_id"]
    if isinstance(config, dict) and "id" in config:
        return _registry_snapshot_target(data, config)
    if data["domain"] == "entity_registry" or data.get("kind") == "entity_registry":
        raise InvalidBackupSnapshotError(
            "Entity registry snapshot has no durable identity"
        )
    if isinstance(config, dict) and config.get("entity_id", target) != target:
        raise InvalidBackupSnapshotError(
            "Entity state snapshot does not match its entity_id"
        )
    if isinstance(config, dict) and config.get("state") is None:
        raise InvalidBackupSnapshotError(
            "Entity state snapshot does not specify a state"
        )
    if not isinstance(config, (dict, str)):
        raise InvalidBackupSnapshotError(
            "Entity state snapshot must contain a state value"
        )
    return data


def _registry_snapshot_target(
    data: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    identity = entity_registry_identity(config)["id"]
    target = config["entity_id"] if data["domain"] == "entity" else identity
    if (
        "state" in config
        or data["entity_id"] != target
        or data.get("kind") not in (None, "dict", "entity_registry")
    ):
        raise InvalidBackupSnapshotError(
            "Entity registry snapshot does not match its identity or format"
        )
    for key in ("options", "categories"):
        if not isinstance(config.get(key), dict):
            raise InvalidBackupSnapshotError(
                f"Entity registry snapshot has invalid {key}"
            )
    if any(key not in config for key in _USER_FIELDS):
        raise InvalidBackupSnapshotError(
            "Entity registry snapshot has incomplete user settings"
        )
    return {**data, "domain": "entity_registry", "entity_id": identity}


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
