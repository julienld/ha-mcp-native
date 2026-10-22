"""Restore a known calendar occurrence without guessing that a UID was deleted."""

import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ha_mcp.runtime.calendar_values import _calendar_bound
from ha_mcp.runtime.snapshot_payloads import (
    BackupRestoreError,
    InvalidBackupSnapshotError,
)
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .calendar_events import CalendarTarget, NativeCalendars
from .registry_access import authorize_actor


def _identity(identifier: str, config: Any) -> tuple[str, str, str | None]:
    entity_id, _, rest = identifier.partition("::")
    uid, _, recurrence_id = rest.partition("::")
    if (
        not isinstance(config, dict)
        or not entity_id.startswith("calendar.")
        or not uid
        or config.get("calendar_entity_id", entity_id) != entity_id
        or config.get("uid", uid) != uid
        or (recurrence_id and config.get("recurrence_id") != recurrence_id)
    ):
        raise InvalidBackupSnapshotError(
            "Calendar snapshot does not match its identity"
        )
    return entity_id, uid, recurrence_id or config.get("recurrence_id") or None


def _values(identifier: str, config: dict[str, Any]) -> dict[str, Any]:
    entity_id, uid, recurrence_id = _identity(identifier, config)
    start, end = (
        _calendar_bound(config.get("start")),
        _calendar_bound(config.get("end")),
    )
    if start is None or end is None:
        raise InvalidBackupSnapshotError("Calendar snapshot has no usable start/end")
    return {
        "entity_id": entity_id,
        "uid": uid,
        "recurrence_id": recurrence_id,
        "recurrence_range": None,
        "summary": config.get("summary") or "",
        "start": start[0],
        "end": end[0],
        "description": config.get("description") or "",
        "location": config.get("location") or "",
        # Expanded occurrences carry their series' rule. Replaying it would
        # create a second recurring series instead of restoring this occurrence.
        "rrule": None,
    }


def _bound(value: Any) -> Any:
    bound = _calendar_bound(value)
    if bound is None or bound[1]:
        return bound
    parsed = datetime.fromisoformat(bound[0])
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.isoformat(), False


def _matches(current: dict[str, Any], saved: dict[str, Any]) -> bool:
    return all(
        (current.get(key) or "") == (saved.get(key) or "")
        for key in ("summary", "description", "location")
    ) and all(
        _bound(current.get(key)) == _bound(saved.get(key)) for key in ("start", "end")
    )


@dataclass(slots=True)
class NativeCalendarRestore:
    service: NativeCalendars
    identifier: str
    config: dict[str, Any]
    target: CalendarTarget | None = field(default=None, init=False)

    async def current(self) -> dict[str, Any] | None:
        entity_id, uid, recurrence_id = _identity(self.identifier, self.config)
        await authorize_actor(self.service.actor, self.service.authorize)
        if self.target is None:
            self.target = self.service.target(entity_id)
        self.service.check_target(self.target)
        return (await self.service.observe(self.target, uid, recurrence_id)).value

    async def apply(self, before: dict[str, Any] | None) -> dict[str, Any]:
        values = _values(self.identifier, self.config)
        async with registry_update_lock("calendar_event", values["entity_id"]):
            current = await self.current()
            if not json_values_equal(current, before):
                raise BackupRestoreError(
                    "The calendar occurrence changed after its safety image was read.",
                    reason="config_conflict",
                )
            if current is None:
                raise BackupRestoreError(
                    "The saved UID was not found in Core's bounded occurrence query. "
                    "Automatic restore cannot prove deletion and will not create a possible duplicate. "
                    "The saved values remain available for an explicit creation.",
                    reason="calendar_identity_unavailable",
                    manual_create_arguments={
                        key: value
                        for key, value in values.items()
                        if key
                        not in {"uid", "recurrence_id", "recurrence_range", "rrule"}
                    },
                )
            if _matches(current, self.config):
                return {
                    "restore_mode": "already_matches",
                    "event": current,
                    "verification_status": "matched",
                }
            assert self.target is not None
            event = self.service._event_values(values)
            await self.service._dispatch(self.target, "update", event, values)
            try:
                restored = await self.current()
            except Exception as error:
                raise BackupRestoreError(
                    "The calendar update completed, but its result could not be read.",
                    apply_status="applied",
                    verification_status="unavailable",
                ) from error
            if restored is None or not _matches(restored, self.config):
                raise BackupRestoreError(
                    "The calendar update completed, but the saved fields were not confirmed.",
                    apply_status="applied",
                    verification_status="mismatched" if restored else "unavailable",
                )
            return {
                "restore_mode": "updated",
                "event": copy.deepcopy(restored),
                "verification_status": "matched",
            }
