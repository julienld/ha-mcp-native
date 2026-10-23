"""Calendar tools call the actual CalendarEntity, without a local API client."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.calendar_values import (
    _calendar_event_backup_id,
    _recurrence_id_window,
    _validate_recurrence_target,
    is_date_only,
)
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.runtime.validation import validate_identifier_not_empty
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .registry_access import (
    Authorize,
    Capture,
    admit_registry_write,
    authorize_actor,
    capture_registry_before,
    registry_failure,
)
from .registry_failures import with_recovery_hint


@dataclass(frozen=True, slots=True)
class CalendarTarget:
    component: Any
    entity: Any
    entity_id: str
    unique_id: str | None


@dataclass(frozen=True, slots=True)
class CalendarObservation:
    value: dict[str, Any] | None
    window: tuple[datetime, datetime] | None = None


@dataclass(slots=True)
class NativeCalendars:
    hass: Any
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)
    backup: Capture | None = field(default=None, kw_only=True, repr=False)
    lookahead_days: int = field(default=7, kw_only=True)

    def target(self, entity_id: str) -> CalendarTarget:
        from homeassistant.components.calendar import DATA_COMPONENT, CalendarEntity

        if not entity_id.startswith("calendar."):
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Calendar entity_id must start with 'calendar.'.",
                status="not_started",
            )
        component = self.hass.data.get(DATA_COMPONENT)
        entity = component.get_entity(entity_id) if component is not None else None
        if not isinstance(entity, CalendarEntity):
            raise ToolFailure(
                "RESOURCE_NOT_FOUND", "Calendar entity not found.", status="not_started"
            )
        return CalendarTarget(component, entity, entity_id, entity.unique_id)

    def check_target(self, target: CalendarTarget) -> None:
        current = self.target(target.entity_id)
        if (
            current.component is not target.component
            or current.entity is not target.entity
            or current.unique_id != target.unique_id
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The calendar owner changed during the operation.",
                status="not_started",
            )

    @staticmethod
    def check_feature(target: CalendarTarget, action: str) -> None:
        from homeassistant.components.calendar import CalendarEntityFeature

        feature = getattr(CalendarEntityFeature, f"{action.upper()}_EVENT")
        if not (target.entity.supported_features or 0) & feature:
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                f"This calendar does not support event {action}.",
                status="not_started",
            )

    async def events(
        self, target: CalendarTarget, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        from homeassistant.components.calendar import api_event_dict_factory

        events = await await_owned_io(
            target.entity.async_get_events(self.hass, start, end)
        )
        await authorize_actor(self.actor, self.authorize)
        self.check_target(target)
        rows: list[dict[str, Any]] = json.loads(
            json_bytes(
                [asdict(event, dict_factory=api_event_dict_factory) for event in events]
            )
        )
        return rows

    async def get(
        self,
        entity_id: str,
        start: str | None = None,
        end: str | None = None,
        max_results: int = 20,
    ) -> dict[str, Any]:
        from homeassistant.util import dt as dt_util

        try:
            await authorize_actor(self.actor, self.authorize)
            target = self.target(entity_id)
            now = dt_util.now()
            start = start if start is not None else now.isoformat()
            end = end if end is not None else (now + timedelta(days=7)).isoformat()
            lower, upper = dt_util.parse_datetime(start), dt_util.parse_datetime(end)
            if lower is None or upper is None or lower > upper:
                raise ToolFailure(
                    "VALIDATION_INVALID_PARAMETER",
                    "Calendar start/end must be valid ISO datetimes in chronological order.",
                    status="not_started",
                )
            rows = await self.events(
                target, dt_util.as_local(lower), dt_util.as_local(upper)
            )
            limited = rows[:max_results]
            return {
                "success": True,
                "entity_id": entity_id,
                "events": limited,
                "count": len(limited),
                "total_available": len(rows),
                "time_range": {"start": start, "end": end},
                "message": f"Retrieved {len(limited)} event(s) from {entity_id}",
            }
        except Exception as error:
            raise registry_failure(error, {"entity_id": entity_id}) from error

    async def observe(
        self, target: CalendarTarget, uid: str, recurrence_id: str | None
    ) -> CalendarObservation:
        """Read an occurrence within explicit bounds; Core has no get-by-UID API."""
        from homeassistant.util import dt as dt_util

        now = dt_util.utcnow()
        days = max(1, min(365, self.lookahead_days))
        windows = [
            (now, now + timedelta(days=days)),
            (now - timedelta(days=366), now + timedelta(days=732)),
        ]
        if around := _recurrence_id_window(recurrence_id):
            windows.insert(0, around)
        for window in windows:
            value = await self.occurrence(target, uid, recurrence_id, window)
            if value is not None:
                return CalendarObservation(value, window)
        return CalendarObservation(None)

    async def occurrence(
        self,
        target: CalendarTarget,
        uid: str,
        recurrence_id: str | None,
        window: tuple[datetime, datetime],
    ) -> dict[str, Any] | None:
        for event in await self.events(target, *window):
            if event.get("uid") != uid:
                continue
            if (
                recurrence_id is not None
                and event.get("recurrence_id") != recurrence_id
            ):
                continue
            if recurrence_id is None and event.get("rrule"):
                return None
            return {"calendar_entity_id": target.entity_id, **event}
        return None

    async def read_snapshot(self, identifier: str) -> dict[str, Any] | None:
        entity_id, _, rest = identifier.partition("::")
        uid, _, recurrence_id = rest.partition("::")
        if not uid:
            return None
        await authorize_actor(self.actor, self.authorize)
        target = self.target(entity_id)
        return (await self.observe(target, uid, recurrence_id or None)).value

    async def _capture(
        self, target: CalendarTarget, values: dict[str, Any], source: str
    ) -> list[str]:
        uid = values.get("uid")
        if uid is None:
            return []
        identifier = _calendar_event_backup_id(values)
        if not identifier:
            return [
                "No restorable occurrence snapshot is available for a recurrence-rule or ranged write."
            ]
        recurrence_id = values.get("recurrence_id")
        try:
            observed = await self.observe(target, uid, recurrence_id)
        except ToolFailure:
            raise
        except Exception as error:  # noqa: BLE001 - best-effort capture must retain write-only providers
            await authorize_actor(self.actor, self.authorize)
            self.check_target(target)
            return [
                f"The calendar occurrence could not be captured ({type(error).__name__}); no restorable snapshot was saved."
            ]
        if observed.value is None:
            return [
                "No occurrence snapshot was captured: Core exposes bounded occurrences, not a complete event series."
            ]
        warnings = await capture_registry_before(
            self.backup,
            self.actor,
            "calendar_event",
            identifier,
            observed.value,
            source,
        )
        assert observed.window is not None
        current = await self.occurrence(target, uid, recurrence_id, observed.window)
        if not json_values_equal(current, observed.value):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The observed calendar occurrence changed during its safety capture.",
                status="not_started",
            )
        return warnings

    @staticmethod
    def _event_values(values: dict[str, Any]) -> dict[str, Any]:
        from homeassistant.components.calendar import WEBSOCKET_EVENT_SCHEMA

        start, end = values["start"], values["end"]
        if is_date_only(start) != is_date_only(end):
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Calendar event start and end must both be dates or both be datetimes.",
                status="not_started",
            )
        event = {"summary": values["summary"], "dtstart": start, "dtend": end}
        for key in ("description", "location"):
            if values[key] is not None or values["uid"] is not None:
                event[key] = values[key] or ""
        if values["rrule"] is not None:
            event["rrule"] = values["rrule"]
        result: dict[str, Any] = WEBSOCKET_EVENT_SCHEMA(event)
        return result

    async def set(
        self,
        entity_id: str,
        summary: str,
        start: str,
        end: str,
        description: str | None = None,
        location: str | None = None,
        rrule: str | None = None,
        uid: str | None = None,
        recurrence_id: str | None = None,
        recurrence_range: Literal["THISANDFUTURE"] | None = None,
    ) -> dict[str, Any]:
        values = {
            "entity_id": entity_id,
            "summary": summary,
            "start": start,
            "end": end,
            "description": description,
            "location": location,
            "rrule": rrule,
            "uid": uid,
            "recurrence_id": recurrence_id,
            "recurrence_range": recurrence_range,
        }
        try:
            await admit_registry_write(self.actor, self.authorize)
            if uid is not None:
                validate_identifier_not_empty(uid, "uid")
            elif recurrence_id is not None or recurrence_range is not None:
                raise ToolFailure(
                    "VALIDATION_INVALID_PARAMETER",
                    "Recurrence targeting requires uid to update an existing event.",
                    status="not_started",
                )
            _validate_recurrence_target(entity_id, recurrence_id, recurrence_range)
            event = self._event_values(values)
            action = "create" if uid is None else "update"
            async with registry_update_lock("calendar_event", entity_id):
                target = self.target(entity_id)
                self.check_feature(target, action)
                warnings = await self._capture(
                    target, values, "ha_config_set_calendar_event"
                )
                await self._dispatch(target, action, event, values)
            details = {
                key: values[key]
                for key in (
                    "summary",
                    "start",
                    "end",
                    "description",
                    "location",
                    "rrule",
                )
            }
            result: dict[str, Any] = {
                "success": True,
                "entity_id": entity_id,
                "event": details,
                "result": None,
                "message": f"Successfully {action}d event '{uid or summary}' in {entity_id}",
            }
            if uid is not None:
                result.update(
                    uid=uid,
                    recurrence_id=recurrence_id,
                    recurrence_range=recurrence_range,
                )
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as error:
            raise registry_failure(
                error, {"entity_id": entity_id, "uid": uid}
            ) from error

    async def remove(
        self,
        entity_id: str,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: Literal["THISANDFUTURE"] | None = None,
    ) -> dict[str, Any]:
        values = {
            "entity_id": entity_id,
            "uid": uid,
            "recurrence_id": recurrence_id,
            "recurrence_range": recurrence_range,
        }
        try:
            await admit_registry_write(self.actor, self.authorize)
            validate_identifier_not_empty(uid, "uid")
            _validate_recurrence_target(entity_id, recurrence_id, recurrence_range)
            async with registry_update_lock("calendar_event", entity_id):
                target = self.target(entity_id)
                self.check_feature(target, "delete")
                warnings = await self._capture(
                    target, values, "ha_config_remove_calendar_event"
                )
                await self._dispatch(target, "delete", None, values)
            result: dict[str, Any] = {
                "success": True,
                **values,
                "result": None,
                "message": f"Successfully deleted event '{uid}' from {entity_id}",
            }
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as error:
            raise registry_failure(
                error, {"entity_id": entity_id, "uid": uid}
            ) from error

    async def _dispatch(
        self,
        target: CalendarTarget,
        action: str,
        event: dict[str, Any] | None,
        values: dict[str, Any],
    ) -> None:
        await await_owned_io(self._invoke_provider(target, action, event, values))

    async def _invoke_provider(
        self,
        target: CalendarTarget,
        action: str,
        event: dict[str, Any] | None,
        values: dict[str, Any],
    ) -> None:
        # Admission runs inside the owned task, after its scheduling boundary.
        await admit_registry_write(self.actor, self.authorize)
        self.check_target(target)
        self.check_feature(target, action)
        check_mutation_admission(self.actor)
        try:
            if action == "create":
                assert event is not None
                operation = target.entity.async_create_event(**event)
            else:
                targeting = {
                    "recurrence_id": values.get("recurrence_id"),
                    "recurrence_range": values.get("recurrence_range"),
                }
                operation = (
                    target.entity.async_update_event(values["uid"], event, **targeting)
                    if action == "update"
                    else target.entity.async_delete_event(values["uid"], **targeting)
                )
            await operation
        except Exception as error:  # Provider may raise after storing the event.
            raise with_recovery_hint(
                ToolFailure(
                    "OUTCOME_UNKNOWN",
                    f"The calendar provider raised {type(error).__name__} after {action} was dispatched. Inspect the calendar before retrying.",
                    status="outcome_unknown",
                ),
                "Inspect the calendar event and confirm its current state before retrying; the dispatched change may already have been applied",
            ) from error
