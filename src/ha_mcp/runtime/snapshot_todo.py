"""Restore todo values using Core service fields and stable item identities."""

from __future__ import annotations

import datetime
from typing import Any

from ha_mcp.tool_api import Backend, CallContext, ToolFailure
from ha_mcp.utils.config_hash import json_values_equal

from .snapshot_payloads import BackupRestoreError, InvalidBackupSnapshotError
from .snapshot_restore import ApplyStatus
from .todo_targets import todo_target


def todo_identity(entity_id: str, config: Any) -> tuple[str, str]:
    entity, _, reference = entity_id.partition("::")
    if (
        not isinstance(config, dict)
        or not reference
        or not entity.startswith("todo.")
        or config.get("todo_entity_id", entity) != entity
    ):
        raise InvalidBackupSnapshotError("Todo snapshot does not match its list")
    uid = config.get("uid")
    if not isinstance(uid, str) or not uid:
        raise InvalidBackupSnapshotError("Todo snapshot has no stable item UID")
    return entity, uid


async def todo_items(
    backend: Backend, actor: CallContext, entity: str
) -> list[dict[str, Any]]:
    result = await backend.execute("todo.items", {"entity_id": entity}, actor)
    if not result.get("success"):
        raise ToolFailure(
            "RESOURCE_NOT_FOUND", "Todo list is unavailable", status="not_started"
        )
    items: list[dict[str, Any]] = result["result"]["items"]
    return items


async def current_todo(
    backend: Backend, actor: CallContext, entity_id: str, config: Any
) -> dict[str, Any] | None:
    entity, uid = todo_identity(entity_id, config)
    current = next(
        (
            item
            for item in await todo_items(backend, actor, entity)
            if item.get("uid") == uid
        ),
        None,
    )
    return {"todo_entity_id": entity, **current} if current is not None else None


def _fields(config: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    summary = config.get("summary")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or config.get("status") not in {"needs_action", "completed"}
    ):
        raise InvalidBackupSnapshotError("Todo snapshot has invalid summary or status")
    fields: dict[str, Any] = {}
    if "description" in config or (current and "description" in current):
        fields["description"] = config.get("description")
    due = config.get("due")
    if due is not None:
        if not isinstance(due, str):
            raise InvalidBackupSnapshotError("Todo snapshot has an invalid due date")
        try:
            if len(due) == 10:
                datetime.date.fromisoformat(due)
                fields["due_date"] = due
            else:
                datetime.datetime.fromisoformat(due)
                fields["due_datetime"] = due
        except ValueError as error:
            raise InvalidBackupSnapshotError(
                "Todo snapshot has an invalid due date"
            ) from error
    elif current and current.get("due") is not None:
        fields["due_date" if len(current["due"]) == 10 else "due_datetime"] = None
    return fields


async def restore_todo(
    backend: Backend,
    actor: CallContext,
    entity_id: str,
    config: dict[str, Any],
    before: dict[str, Any] | None,
) -> dict[str, Any]:
    """Recreate missing UIDs, or update the captured UID, then verify public fields."""
    entity, uid = todo_identity(entity_id, config)
    fields = _fields(config, before)
    items = await todo_items(backend, actor, entity)
    current = next((item for item in items if item.get("uid") == uid), None)
    expected = (
        {key: value for key, value in before.items() if key != "todo_entity_id"}
        if before is not None
        else None
    )
    if not json_values_equal(current, expected):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "The todo item changed after its snapshot was read",
            status="not_started",
        )
    applied = False
    try:
        if current is None:
            matching = [
                item
                for item in items
                if all(
                    json_values_equal(item.get(key), config.get(key))
                    for key in ("summary", "status", "description", "due")
                )
            ]
            if matching:
                return {
                    "restore_mode": "already_recreated",
                    "item": matching[0],
                    "verification_status": "matched",
                    "warnings": [
                        "An existing item matches every saved field and was left unchanged. "
                        "Todo lists allow identical items; the removed UID was not recreated again."
                    ],
                }
            await backend.execute(
                "todo.item.add",
                {"data": {"entity_id": entity, "item": config["summary"], **fields}},
                actor,
            )
            applied = True
            old_ids = {item.get("uid") for item in items}
            added = [
                item
                for item in await todo_items(backend, actor, entity)
                if item.get("uid") not in old_ids
                and item.get("summary") == config["summary"]
            ]
            if len(added) != 1:
                raise BackupRestoreError(
                    "The todo item was created, but its new UID could not be verified",
                    apply_status="applied",
                    verification_status="unavailable",
                )
            current = added[0]
            uid = current["uid"]
            mode = "recreated"
        else:
            mode = "updated"
        if mode == "updated" or config["status"] != current.get("status"):
            target = todo_target(await todo_items(backend, actor, entity), uid)
            if not json_values_equal(target["current_item"], current):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The todo item changed during restoration",
                    status="not_started",
                )
            await backend.execute(
                "todo.item.update",
                {
                    "data": {
                        "entity_id": entity,
                        "item": target["selector"],
                        "rename": config["summary"],
                        "status": config["status"],
                        **fields,
                    },
                    "expected_item": current,
                },
                actor,
            )
            applied = True
        saved = next(
            (
                item
                for item in await todo_items(backend, actor, entity)
                if item.get("uid") == uid
            ),
            None,
        )
        keys = ("summary", "status", "description", "due")
        if saved is None or any(
            not json_values_equal(saved.get(key), config.get(key)) for key in keys
        ):
            raise BackupRestoreError(
                "The todo restore was sent, but readback did not match the snapshot",
                apply_status="applied",
                verification_status="mismatched",
            )
        return {"restore_mode": mode, "item": saved, "verification_status": "matched"}
    except BackupRestoreError:
        raise
    except Exception as error:
        status: ApplyStatus = (
            "applied"
            if applied
            else "not_applied"
            if isinstance(error, ToolFailure) and error.status == "not_started"
            else "unknown"
        )
        raise BackupRestoreError(
            "Todo restoration did not complete; inspect the list before retrying",
            apply_status=status,
            verification_status="unavailable" if applied else "not_run",
        ) from error
