"""Registry snapshots for the retained standalone tools; no synthetic state writes."""

import logging
from typing import Any

from ha_mcp.errors import HomeAssistantError
from ha_mcp.runtime.entity_snapshot_values import (
    entity_registry_identity,
    entity_restore_payloads,
)
from ha_mcp.runtime.snapshot_payloads import (
    BackupRestoreError,
    InvalidBackupSnapshotError,
)
from ha_mcp.utils.config_hash import json_values_equal

_LOGGER = logging.getLogger(__name__)
_CURRENT = object()


async def _read(client: Any, command: str, **arguments: Any) -> Any:
    response = await client.send_websocket_message({"type": command, **arguments})
    if not response.get("success"):
        if response.get("error_code") == "not_found":
            return None
        raise HomeAssistantError(
            f"Entity registry request failed: {response.get('error')}"
        )
    return response.get("result")


async def fetch_entity_registry(client: Any, identity: str) -> dict[str, Any] | None:
    target = identity
    if "." not in identity:
        rows = await _read(client, "config/entity_registry/list")
        if not isinstance(rows, list):
            raise HomeAssistantError("Entity registry list did not return entries")
        matches = [
            row for row in rows if isinstance(row, dict) and row.get("id") == identity
        ]
        if len(matches) > 1:
            raise HomeAssistantError(
                "Entity registry returned ambiguous durable identities"
            )
        if not matches:
            return None
        target = matches[0]["entity_id"]
    row = await _read(client, "config/entity_registry/get", entity_id=target)
    if row is None:
        return None
    if not isinstance(row, dict):
        raise HomeAssistantError("Entity registry get did not return an entry")
    entity_registry_identity(row)
    if "." not in identity and row["id"] != identity:
        raise HomeAssistantError("Entity identity changed during registry resolution")
    return row


async def restore_entity_registry(
    client: Any,
    identity: str,
    config: Any,
    *,
    expected: Any = _CURRENT,
) -> Any:
    if not isinstance(config, dict) or config.get("id") != identity:
        raise InvalidBackupSnapshotError(
            "Entity registry snapshot does not match its durable identity"
        )
    current = await fetch_entity_registry(client, identity)
    if current is None:
        raise BackupRestoreError(
            "The saved entity registration no longer exists. Registry history updates existing registrations; it cannot recreate an integration-owned entity.",
            reason="entry_deleted",
        )
    if expected is not _CURRENT and not json_values_equal(current, expected):
        raise BackupRestoreError(
            "The entity registration changed after its safety image was read; no restore was sent.",
            reason="config_conflict",
            apply_status="not_applied",
        )
    if not json_values_equal(
        entity_registry_identity(current), entity_registry_identity(config)
    ):
        raise BackupRestoreError(
            "The entity registration has a different durable identity",
            reason="identity_changed",
        )
    result = None
    confirmed_phases: list[str] = []
    for payload in entity_restore_payloads(config, current):
        verified = await fetch_entity_registry(client, identity)
        if not json_values_equal(verified, current):
            raise BackupRestoreError(
                "The entity registration changed between restore phases; no further update was sent.",
                reason="config_conflict",
                apply_status="unknown" if confirmed_phases else "not_applied",
                confirmed_phases=confirmed_phases,
            )
        response = await client.send_websocket_message(
            {"type": "config/entity_registry/update", **payload}
        )
        if not response.get("success"):
            raise HomeAssistantError(
                f"Entity registry restore failed: {response.get('error')}"
            )
        result = response.get("result")
        written = result.get("entity_entry") if isinstance(result, dict) else None
        if not isinstance(written, dict) or not json_values_equal(
            entity_registry_identity(written), entity_registry_identity(config)
        ):
            raise HomeAssistantError(
                "Entity registry restore did not return the same durable identity"
            )
        current = written
        confirmed_phases.append(payload.get("options_domain", "registry"))
    if confirmed_phases and not json_values_equal(
        await fetch_entity_registry(client, identity), current
    ):
        raise BackupRestoreError(
            "The entity registration changed after the last restore acknowledgement; its final state could not be confirmed.",
            reason="readback_mismatch",
            apply_status="unknown",
            confirmed_phases=confirmed_phases,
        )
    return result


async def capture_entity_registry(client: Any, entity_id: str, source: str) -> None:
    from ha_mcp.backup_manager import _CAPTURE_TRANSIENT_ERRORS, get_backup_manager
    from ha_mcp.config import get_global_settings

    settings = get_global_settings()
    if not settings.enable_auto_backup:
        return
    try:
        row = await fetch_entity_registry(client, entity_id)
        if row is not None:
            await get_backup_manager(client, settings).maybe_snapshot_known(
                "entity_registry",
                row["id"],
                row,
                tool_name=source,
            )
    except _CAPTURE_TRANSIENT_ERRORS as error:
        _LOGGER.warning(
            "Entity-registry pre-write capture failed: %s", type(error).__name__
        )


async def capture_entity_device(client: Any, device_id: str) -> None:
    """The entity tool's rename changes a separate device-registry owner."""
    from ha_mcp.backup_manager import _CAPTURE_TRANSIENT_ERRORS, get_backup_manager
    from ha_mcp.config import get_global_settings

    settings = get_global_settings()
    if not settings.enable_auto_backup:
        return
    try:
        await get_backup_manager(client, settings).maybe_snapshot(
            "device",
            device_id,
            tool_name="ha_set_entity",
        )
    except _CAPTURE_TRANSIENT_ERRORS as error:
        _LOGGER.warning(
            "Entity device pre-write capture failed: %s", type(error).__name__
        )
