"""One calendar tool contract, bound to a native or legacy calendar owner."""

from typing import Annotated, Any, Literal, Protocol

from pydantic import Field


class Calendars(Protocol):
    async def get(
        self,
        entity_id: str,
        start: str | None = None,
        end: str | None = None,
        max_results: int = 20,
    ) -> dict[str, Any]: ...
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
    ) -> dict[str, Any]: ...
    async def remove(
        self,
        entity_id: str,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: Literal["THISANDFUTURE"] | None = None,
    ) -> dict[str, Any]: ...


class CalendarTools:
    def __init__(self, calendars: Calendars) -> None:
        self.calendars = calendars

    async def ha_config_get_calendar_events(
        self,
        entity_id: Annotated[
            str, Field(description="Calendar entity ID (e.g., 'calendar.family')")
        ],
        start: Annotated[
            str | None,
            Field(
                description="Start datetime in ISO format; omit it to start at the current time",
                default=None,
            ),
        ] = None,
        end: Annotated[
            str | None,
            Field(
                description="End datetime in ISO format (default: 7 days from now, "
                "not from start; pass end whenever you pass start)",
                default=None,
            ),
        ] = None,
        max_results: Annotated[
            int,
            Field(description="Maximum number of events to return", default=20),
        ] = 20,
    ) -> dict[str, Any]:
        """Get calendar events from a calendar entity within a time range.

        Returns each event's summary, start, end, description and location,
        plus the uid, recurrence_id and rrule when the calendar provides them
        (the edit and remove tools use uid and recurrence_id). To find calendar
        entities, use ha_search(domain_filter='calendar').

        EXAMPLES:
        - Next week (defaults): ha_config_get_calendar_events("calendar.family")
        - Date range: ha_config_get_calendar_events("calendar.work", start="2024-01-01T00:00:00", end="2024-01-31T23:59:59")
        """
        return await self.calendars.get(
            entity_id=entity_id, start=start, end=end, max_results=max_results
        )

    async def ha_config_set_calendar_event(
        self,
        entity_id: Annotated[
            str, Field(description="Calendar entity ID (e.g., 'calendar.family')")
        ],
        summary: Annotated[str, Field(description="Event title/summary")],
        start: Annotated[
            str, Field(description="Event start date or datetime in ISO format")
        ],
        end: Annotated[
            str,
            Field(
                description=(
                    "Event end date or datetime in ISO format. For all-day "
                    "events (date-only) the end date is exclusive; a "
                    "single-day all-day event needs end = start + 1 day."
                )
            ),
        ],
        description: Annotated[
            str | None,
            Field(description="Event description", default=None),
        ] = None,
        location: Annotated[
            str | None, Field(description="Event location", default=None)
        ] = None,
        rrule: Annotated[
            str | None,
            Field(
                description=(
                    "RFC 5545 recurrence rule, without 'RRULE:' prefix (e.g., "
                    "'FREQ=WEEKLY;BYDAY=MO' or 'FREQ=MONTHLY;BYDAY=3SA'). Creates a "
                    "recurring event series."
                ),
                default=None,
            ),
        ] = None,
        uid: Annotated[
            str | None,
            Field(
                description=(
                    "UID of an existing event to update. Omit to create a new event."
                ),
                default=None,
            ),
        ] = None,
        recurrence_id: Annotated[
            str | None,
            Field(
                description=(
                    "Only meaningful with 'uid': identifies one occurrence of a "
                    "recurring series to update."
                ),
                default=None,
            ),
        ] = None,
        recurrence_range: Annotated[
            Literal["THISANDFUTURE"] | None,
            Field(
                description=(
                    "Only meaningful with 'uid': 'THISANDFUTURE' to update "
                    "this and all following occurrences. Home Assistant "
                    "compares this value verbatim, so no other spelling "
                    "(including 'THIS_AND_FUTURE') selects the range."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create a new event in a calendar, or update an existing one.

        Creates a one-off event via the calendar.create_event service, or a
        recurring series via the WebSocket ``calendar/event/create`` command
        when ``rrule`` is provided (the REST service schema does not accept
        recurrence rules). Passing ``uid`` switches to update mode, which uses
        the WebSocket ``calendar/event/update`` command — HA registers no REST
        service for updating an event.

        **When NOT to use:**
        - To retrieve calendar events, use ``ha_config_get_calendar_events``.
        - To delete an event, use ``ha_config_remove_calendar_event``.
        - To find the ``uid`` of an event to update, use
          ``ha_config_get_calendar_events``; this tool does not search.

        Passing date-only values (``YYYY-MM-DD``) for both ``start`` and
        ``end`` creates an all-day event; passing full ISO datetimes creates
        a timed event. The two forms cannot be mixed — a date-only ``start``
        with a datetime ``end`` (or vice versa) is rejected.

        An update replaces the whole event rather than patching it, so
        ``summary``, ``start`` and ``end`` stay required in update mode, and a
        ``description`` or ``location`` that is not re-supplied is cleared.
        An ``rrule`` is the exception: Home Assistant accepts a new rule but
        has no way to express "no recurrence", so an existing rule survives an
        update that omits it. Delete the event and create it again to drop the
        recurrence.

        Not every calendar integration supports event creation; recurring
        events additionally require the integration to support recurrence
        (the built-in Local Calendar does). Update support is narrower still:
        Local Calendar implements it, while the core Google Calendar and CalDAV
        integrations do not.

        EXAMPLES:
        - Create: ha_config_set_calendar_event("calendar.family", summary="Doctor appointment", start="2024-01-15T14:00:00", end="2024-01-15T15:00:00")
        - Recurring (every Monday, 10 occurrences): ha_config_set_calendar_event("calendar.work", summary="Team meeting", start="2024-01-15T10:00:00", end="2024-01-15T11:00:00", rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10")
        - Update this and all later occurrences: ha_config_set_calendar_event("calendar.work", summary="Team meeting (new time)", start="2024-02-05T11:00:00", end="2024-02-05T12:00:00", uid="recurring-event-67890", recurrence_id="20240205T100000", recurrence_range="THISANDFUTURE")
        """
        return await self.calendars.set(
            entity_id=entity_id,
            summary=summary,
            start=start,
            end=end,
            description=description,
            location=location,
            rrule=rrule,
            uid=uid,
            recurrence_id=recurrence_id,
            recurrence_range=recurrence_range,
        )

    async def ha_config_remove_calendar_event(
        self,
        entity_id: Annotated[
            str, Field(description="Calendar entity ID (e.g., 'calendar.family')")
        ],
        uid: Annotated[
            str, Field(description="Unique identifier of the event to delete")
        ],
        recurrence_id: Annotated[
            str | None,
            Field(description="Recurrence ID for recurring events", default=None),
        ] = None,
        recurrence_range: Annotated[
            Literal["THISANDFUTURE"] | None,
            Field(
                description=(
                    "Recurrence range: 'THISANDFUTURE' to delete this and future "
                    "occurrences. Home Assistant compares this value verbatim, so no other "
                    "spelling (including 'THIS_AND_FUTURE') selects the range."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Delete an event from a calendar.

        Deletes a calendar event via the WebSocket ``calendar/event/delete``
        command. HA's calendar component only registers ``create_event`` and
        ``get_events`` as REST services — delete and update live on the
        WebSocket API only. Get the event UID from
        ha_config_get_calendar_events().

        EXAMPLES:
        - Delete a single event: ha_config_remove_calendar_event("calendar.family", uid="event-12345")
        - Delete one occurrence and all later ones: ha_config_remove_calendar_event("calendar.work", uid="recurring-event-67890", recurrence_id="20240115T100000", recurrence_range="THISANDFUTURE")
        """
        return await self.calendars.remove(
            entity_id=entity_id,
            uid=uid,
            recurrence_id=recurrence_id,
            recurrence_range=recurrence_range,
        )
