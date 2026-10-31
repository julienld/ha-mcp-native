"""
Calendar event management tools for Home Assistant MCP server.

This module provides tools for managing calendar events in Home Assistant,
including retrieving events, creating events, updating existing events, and
deleting events.

Use ha_search(query='calendar', domain_filter='calendar') to find calendar entities.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Literal

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors_sync
from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from ..runtime.calendar_tools import Calendars
from ..runtime.calendar_tools import CalendarTools as _CalendarContractTools
from ..runtime.calendar_values import (
    _calendar_event_backup_id as _calendar_event_backup_id,
)
from ..runtime.calendar_values import (
    _validate_recurrence_target as _pure_validate_recurrence_target,
)
from ..runtime.calendar_values import is_date_only
from .auto_backup import with_auto_backup
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .tool_hints import read_only_hints, write_hints
from .util_helpers import is_connection_error_message

logger = logging.getLogger(__name__)
_validate_recurrence_target = legacy_errors_sync(_pure_validate_recurrence_target)

# The only recurrence range Home Assistant recognises; it compares the string
# verbatim against ical's Range.THIS_AND_FUTURE.


class LegacyCalendars:
    """Calendar event management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def get(
        self,
        entity_id: str,
        start: str | None = None,
        end: str | None = None,
        max_results: int = 20,
    ) -> dict[str, Any]:
        try:
            # Validate entity_id
            if not entity_id.startswith("calendar."):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid calendar entity ID: {entity_id}. Must start with 'calendar.'",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Use ha_search(query='calendar', domain_filter='calendar') to find calendar entities",
                            "Calendar entity IDs start with 'calendar.' prefix",
                        ],
                    )
                )

            # Set default time range if not provided
            now = datetime.now()
            if start is None:
                start = now.isoformat()
            if end is None:
                end_date = now + timedelta(days=7)
                end = end_date.isoformat()

            # Build the API endpoint for calendar events
            # Home Assistant uses: GET /api/calendars/{entity_id}?start=...&end=...
            params = {"start": start, "end": end}

            # Use the REST client to fetch calendar events
            # The endpoint is /calendars/{entity_id} (note: without /api prefix as client adds it)
            response = await self._client._request(
                "GET", f"/calendars/{entity_id}", params=params
            )

            # Response is a list of events
            events = response if isinstance(response, list) else []

            # Limit results
            limited_events = events[:max_results]

            return {
                "success": True,
                "entity_id": entity_id,
                "events": limited_events,
                "count": len(limited_events),
                "total_available": len(events),
                "time_range": {
                    "start": start,
                    "end": end,
                },
                "message": f"Retrieved {len(limited_events)} event(s) from {entity_id}",
            }

        except ToolError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.error(f"Failed to get calendar events for {entity_id}: {error}")

            # Provide helpful error messages
            suggestions = [
                f"Verify calendar entity '{entity_id}' exists using ha_search(query='calendar', domain_filter='calendar')",
                "Check start/end datetime format (ISO 8601)",
                "Ensure calendar integration supports event retrieval",
            ]

            error_str = str(error)
            if "404" in error_str or "not found" in error_str.lower():
                suggestions.insert(0, f"Calendar entity '{entity_id}' not found")

            exception_to_structured_error(
                error, context={"entity_id": entity_id}, suggestions=suggestions
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _create_recurring_calendar_event(
        self,
        entity_id: str,
        summary: str,
        start: str,
        end: str,
        description: str | None,
        location: str | None,
        rrule: str,
    ) -> Any:
        """Create a recurring calendar event series via the WebSocket API.

        The ``calendar.create_event`` service schema has no rrule field --
        recurrence is only accepted by the WebSocket command
        ``calendar/event/create`` (see HA Core
        ``homeassistant/components/calendar/__init__.py``), the same split
        that forces the delete tool below onto the WebSocket API.
        """
        event: dict[str, Any] = {
            "summary": summary,
            "dtstart": start,
            "dtend": end,
            "rrule": rrule,
        }
        if description is not None:
            event["description"] = description
        if location is not None:
            event["location"] = location

        # Route through the shared pooled WebSocket (issue #1813) instead of a
        # dedicated connect/auth handshake per call. The pooled path collapses a
        # failed WS command into ``{"success": False, "error": ...}``; a
        # transport-shaped failure re-raises as ``HomeAssistantConnectionError``
        # (the classifier type-matches it to connectivity guidance), anything
        # else as the same ``HomeAssistantCommandError`` the dedicated
        # send_command used to raise so the caller's error handler still
        # attaches the rrule-specific suggestions.
        result = await self._client.send_websocket_message(
            {"type": "calendar/event/create", "entity_id": entity_id, "event": event}
        )
        if not result.get("success"):
            error = str(result.get("error", "calendar/event/create failed"))
            if is_connection_error_message(error):
                raise HomeAssistantConnectionError(error)
            raise HomeAssistantCommandError(error)
        return result

    async def _create_simple_calendar_event(
        self,
        entity_id: str,
        summary: str,
        start: str,
        end: str,
        description: str | None,
        location: str | None,
    ) -> Any:
        """Create a one-off calendar event via the calendar.create_event service."""
        start_is_date = self._is_date_only(start)

        service_data: dict[str, Any] = {
            "entity_id": entity_id,
            "summary": summary,
        }
        if start_is_date:
            service_data.update({"start_date": start, "end_date": end})
        else:
            service_data.update({"start_date_time": start, "end_date_time": end})

        if description is not None:
            service_data["description"] = description
        if location is not None:
            service_data["location"] = location

        return await self._client.call_service("calendar", "create_event", service_data)

    async def _update_calendar_event(
        self,
        entity_id: str,
        uid: str,
        summary: str,
        start: str,
        end: str,
        description: str | None,
        location: str | None,
        rrule: str | None,
        recurrence_id: str | None,
        recurrence_range: str | None,
    ) -> Any:
        """Update an existing calendar event via the WebSocket API.

        HA registers only ``create_event`` and ``get_events`` as REST services
        (see HA Core ``homeassistant/components/calendar/__init__.py``);
        ``calendar/event/update`` is WebSocket-only, the same split that forces
        the delete tool onto the WebSocket API.
        """
        # HA merges rather than replaces, so an omitted key keeps its old
        # value; the empty strings are what make this a replacement.
        event: dict[str, Any] = {
            "summary": summary,
            "dtstart": start,
            "dtend": end,
            "description": description or "",
            "location": location or "",
        }
        if rrule is not None:
            # No empty counterpart: HA's rule parser rejects null and empty.
            event["rrule"] = rrule

        ws_kwargs: dict[str, Any] = {"entity_id": entity_id, "uid": uid}
        if recurrence_id:
            ws_kwargs["recurrence_id"] = recurrence_id
        if recurrence_range:
            ws_kwargs["recurrence_range"] = recurrence_range

        # Same pooled-WebSocket routing and failure mapping as the create and
        # delete paths (issue #1813): a transport-shaped failure becomes
        # ``HomeAssistantConnectionError`` so the classifier attaches
        # connectivity guidance, anything else ``HomeAssistantCommandError`` so
        # the caller's handler attaches the update-specific suggestions.
        result = await self._client.send_websocket_message(
            {"type": "calendar/event/update", **ws_kwargs, "event": event}
        )
        if not result.get("success"):
            error = str(result.get("error", "calendar/event/update failed"))
            if is_connection_error_message(error):
                raise HomeAssistantConnectionError(error)
            raise HomeAssistantCommandError(error)
        return result

    @staticmethod
    def _is_date_only(value: str) -> bool:
        return is_date_only(value)

    def _build_set_calendar_event_error_suggestions(
        self,
        entity_id: str,
        rrule: str | None,
        error: Exception,
        start: str,
        end: str,
        uid: str | None = None,
    ) -> list[str]:
        """Build suggestions for a failed ha_config_set_calendar_event call.

        ``uid`` selects update-mode wording; ``None`` keeps the create-mode
        suggestions.
        """
        if isinstance(error, HomeAssistantConnectionError):
            # A transport drop is not a calendar problem — domain hints would
            # send the agent chasing a non-issue during an HA restart.
            return [
                "Home Assistant may be restarting or unreachable — retry shortly",
                "Check the connection to Home Assistant",
            ]
        if uid is not None:
            suggestions = [
                f"Verify calendar entity '{entity_id}' exists and supports event update",
                f"Verify event with UID '{uid}' exists in the calendar",
                "Use ha_config_get_calendar_events() to find the correct event UID",
                "Check datetime format (ISO 8601)",
                "Ensure end time is after start time",
                (
                    "Not every calendar integration supports updating events "
                    "(Local Calendar does; the core Google and CalDAV "
                    "integrations do not)"
                ),
            ]
        else:
            suggestions = [
                f"Verify calendar entity '{entity_id}' exists and supports event creation",
                "Check datetime format (ISO 8601)",
                "Ensure end time is after start time",
                "Some calendar integrations may be read-only",
            ]
        if rrule:
            suggestions.insert(
                0,
                "Check RRULE syntax (RFC 5545, without the 'RRULE:' prefix, "
                "e.g. 'FREQ=WEEKLY;BYDAY=MO') and that this calendar "
                "integration supports recurring events",
            )

        error_str = str(error)
        if "404" in error_str or "not found" in error_str.lower():
            suggestions.insert(
                0,
                f"Calendar entity '{entity_id}' or event '{uid}' not found"
                if uid is not None
                else f"Calendar entity '{entity_id}' not found",
            )
        # Update always travels the WebSocket path, which preserves HA's
        # humanized message verbatim ("Calendar does not support event
        # update"); on the create side that only happens on the WebSocket
        # (rrule) path. Both spellings are matched because HA words the
        # WebSocket message as "does not support" while other layers say "not
        # supported". The REST service path loses the text entirely: a
        # read-only calendar raises ServiceNotSupported, which HA's handler
        # does not map (see homeassistant/helpers/http.py — only vol.Invalid,
        # ServiceNotFound and Unauthorized are), so the client sees a bodyless
        # status line. Do NOT broaden this to match that generic shape: it
        # would promote "does not support event creation" onto every validation
        # failure. The REST path keeps the read-only hint via the unconditional
        # "Some calendar integrations may be read-only" suggestion above.
        lowered = error_str.lower()
        if "not supported" in lowered or "does not support" in lowered:
            suggestions.insert(
                0,
                "This calendar does not support event update"
                if uid is not None
                else "This calendar does not support event creation",
            )
        # HA treats the all-day end date as exclusive and enforces
        # MIN_NEW_EVENT_DURATION (1 second), so an all-day range with
        # end <= start is rejected. Key this on the boundaries we already hold
        # rather than on HA's wording: the REST service path raises a bare
        # HTTPBadRequest, so voluptuous' "Expected minimum event duration"
        # text never reaches the client (only the WebSocket rrule path keeps
        # it). Strict YYYY-MM-DD sorts lexicographically, so comparing the
        # strings is an exact date comparison here.
        if self._is_date_only(start) and self._is_date_only(end) and end <= start:
            suggestions.insert(
                0,
                "For an all-day event the end date is exclusive — set end to "
                "start + 1 day (a single-day all-day event cannot have "
                "start == end)",
            )

        return suggestions

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
        try:
            # Validate entity_id
            if not entity_id.startswith("calendar."):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid calendar entity ID: {entity_id}. Must start with 'calendar.'",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Use ha_search(query='calendar', domain_filter='calendar') to find calendar entities",
                            "Calendar entity IDs start with 'calendar.' prefix",
                        ],
                    )
                )

            if uid is not None:
                # An empty/whitespace uid would reach the WS command and HA
                # returns a misleading "event not found".
                validate_identifier_not_empty(
                    uid,
                    "uid",
                    suggestions=[
                        "Use ha_config_get_calendar_events() to list events and obtain valid UIDs",
                        "Omit uid to create a new event instead of updating one",
                    ],
                    context={"entity_id": entity_id},
                )
            elif recurrence_id is not None or recurrence_range is not None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "recurrence_id and recurrence_range only apply when updating an event (uid is required)",
                        context={
                            "entity_id": entity_id,
                            "recurrence_id": recurrence_id,
                            "recurrence_range": recurrence_range,
                        },
                        suggestions=[
                            "Pass uid to update an existing event's occurrence",
                            "Omit recurrence_id and recurrence_range to create a new event",
                            "Use rrule to create a new recurring series",
                        ],
                    )
                )

            _validate_recurrence_target(entity_id, recurrence_id, recurrence_range)

            # Reject mixed date/datetime up front so the simple, recurring
            # (rrule) and update paths give the same clear validation error —
            # only the simple path routes through
            # ``_create_simple_calendar_event`` where this used to live.
            if self._is_date_only(start) != self._is_date_only(end):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Calendar event start and end must both be dates or both be datetimes",
                        context={"start": start, "end": end},
                        suggestions=[
                            "Use YYYY-MM-DD for both values to create an all-day event",
                            "Use ISO 8601 datetimes for both values to create a timed event",
                        ],
                    )
                )

            event_details: dict[str, Any] = {
                "summary": summary,
                "start": start,
                "end": end,
                "description": description,
                "location": location,
                "rrule": rrule,
            }

            if uid is not None:
                result = await self._update_calendar_event(
                    entity_id,
                    uid,
                    summary,
                    start,
                    end,
                    description,
                    location,
                    rrule,
                    recurrence_id,
                    recurrence_range,
                )
                return {
                    "success": True,
                    "entity_id": entity_id,
                    "uid": uid,
                    "recurrence_id": recurrence_id,
                    "recurrence_range": recurrence_range,
                    "event": event_details,
                    "result": result,
                    "message": f"Successfully updated event '{uid}' in {entity_id}",
                }

            if rrule:
                result = await self._create_recurring_calendar_event(
                    entity_id, summary, start, end, description, location, rrule
                )
            else:
                result = await self._create_simple_calendar_event(
                    entity_id, summary, start, end, description, location
                )

            return {
                "success": True,
                "entity_id": entity_id,
                "event": event_details,
                "result": result,
                "message": f"Successfully created event '{summary}' in {entity_id}",
            }

        except ToolError:
            raise
        except Exception as error:  # noqa: BLE001
            action = "update" if uid is not None else "create"
            logger.error(f"Failed to {action} calendar event in {entity_id}: {error}")

            suggestions = self._build_set_calendar_event_error_suggestions(
                entity_id, rrule, error, start, end, uid
            )

            context: dict[str, Any] = {"entity_id": entity_id}
            if uid is not None:
                context["uid"] = uid

            exception_to_structured_error(
                error, context=context, suggestions=suggestions
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def remove(
        self,
        entity_id: str,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: Literal["THISANDFUTURE"] | None = None,
    ) -> dict[str, Any]:
        try:
            # Validate entity_id
            if not entity_id.startswith("calendar."):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid calendar entity ID: {entity_id}. Must start with 'calendar.'",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Use ha_search(query='calendar', domain_filter='calendar') to find calendar entities",
                            "Calendar entity IDs start with 'calendar.' prefix",
                        ],
                    )
                )

            # entity_id format-check above does not cover the ``uid`` parameter.
            # Empty/whitespace uid would flow through to the WS command and HA
            # returns a misleading "event not found".
            validate_identifier_not_empty(
                uid,
                "uid",
                suggestions=[
                    "Use ha_config_get_calendar_events() to list events and obtain valid UIDs",
                ],
                context={"entity_id": entity_id},
            )

            _validate_recurrence_target(entity_id, recurrence_id, recurrence_range)

            # ``calendar.delete_event`` is NOT a REST service — HA only
            # registers ``calendar.create_event`` and ``calendar.get_events``.
            # Delete is exposed exclusively via the WebSocket command
            # ``calendar/event/delete`` (see HA Core
            # ``homeassistant/components/calendar/__init__.py``).
            ws_kwargs: dict[str, Any] = {"entity_id": entity_id, "uid": uid}
            if recurrence_id:
                ws_kwargs["recurrence_id"] = recurrence_id
            if recurrence_range:
                ws_kwargs["recurrence_range"] = recurrence_range

            # Route through the shared pooled WebSocket (issue #1813) instead of a
            # dedicated connect/auth handshake per call. Transport-shaped
            # failures raise ``HomeAssistantConnectionError`` (connectivity
            # guidance); anything else re-raises as the same
            # ``HomeAssistantCommandError`` the dedicated send_command used to
            # raise so the outer handler builds the delete-specific
            # suggestions (404 / not-supported).
            result = await self._client.send_websocket_message(
                {"type": "calendar/event/delete", **ws_kwargs}
            )
            if not result.get("success"):
                error = str(result.get("error", "calendar/event/delete failed"))
                if is_connection_error_message(error):
                    raise HomeAssistantConnectionError(error)
                raise HomeAssistantCommandError(error)

            return {
                "success": True,
                "entity_id": entity_id,
                "uid": uid,
                "recurrence_id": recurrence_id,
                "recurrence_range": recurrence_range,
                "result": result,
                "message": f"Successfully deleted event '{uid}' from {entity_id}",
            }

        except ToolError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.error(f"Failed to delete calendar event from {entity_id}: {error}")

            exception_to_structured_error(
                error,
                context={"entity_id": entity_id, "uid": uid},
                suggestions=self._build_remove_calendar_event_error_suggestions(
                    entity_id, uid, error
                ),
            )
            return None  # unreachable: exception_to_structured_error always raises

    def _build_remove_calendar_event_error_suggestions(
        self, entity_id: str, uid: str, error: Exception
    ) -> list[str]:
        """Build suggestions for a failed ha_config_remove_calendar_event call."""
        if isinstance(error, HomeAssistantConnectionError):
            # A transport drop is not a calendar problem — domain hints would
            # send the agent chasing a non-issue during an HA restart.
            return [
                "Home Assistant may be restarting or unreachable — retry shortly",
                "Check the connection to Home Assistant",
            ]
        suggestions = [
            f"Verify calendar entity '{entity_id}' exists",
            f"Verify event with UID '{uid}' exists in the calendar",
            "Use ha_config_get_calendar_events() to find the correct event UID",
            "Some calendar integrations may not support event deletion",
        ]
        error_str = str(error)
        if "404" in error_str or "not found" in error_str.lower():
            suggestions.insert(
                0, f"Calendar entity '{entity_id}' or event '{uid}' not found"
            )
        # HA words the WebSocket refusal "Calendar does not support event
        # deletion"; other layers say "not supported". Match both.
        lowered = error_str.lower()
        if "not supported" in lowered or "does not support" in lowered:
            suggestions.insert(0, "This calendar does not support event deletion")
        return suggestions


class CalendarTools(LegacyCalendars):
    @property
    def calendars(self) -> Calendars:
        return self

    ha_config_get_calendar_events = tool(
        name="ha_config_get_calendar_events",
        tags={"Calendar"},
        annotations=read_only_hints("Get Calendar Events", open_world=False),
    )(log_tool_usage(_CalendarContractTools.ha_config_get_calendar_events))

    ha_config_set_calendar_event = tool(
        name="ha_config_set_calendar_event",
        tags={"Calendar"},
        annotations=write_hints(
            "Create or Update Calendar Event",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(
        with_auto_backup(domain="calendar_event", id_fn=_calendar_event_backup_id)(
            log_tool_usage(_CalendarContractTools.ha_config_set_calendar_event)
        )
    )

    ha_config_remove_calendar_event = tool(
        name="ha_config_remove_calendar_event",
        tags={"Calendar"},
        annotations=write_hints(
            "Remove Calendar Event", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(domain="calendar_event", id_fn=_calendar_event_backup_id)(
            log_tool_usage(_CalendarContractTools.ha_config_remove_calendar_event)
        )
    )


def register_calendar_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register calendar management tools with the MCP server."""
    register_tool_methods(mcp, CalendarTools(client))
