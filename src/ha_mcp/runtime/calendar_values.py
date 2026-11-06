"""Shared calendar targeting and snapshot values, without a transport client."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.runtime.validation import validate_identifier_not_empty

logger = logging.getLogger(__name__)
_THIS_AND_FUTURE = "THISANDFUTURE"


def _calendar_event_backup_id(kw: dict[str, Any]) -> str:
    """Auto-backup key for a calendar write: ``<entity>::<uid>[::<recurrence_id>]``.

    A recurring series expands into occurrences that all share the ``uid`` and
    differ only by ``recurrence_id``, so the uid alone cannot identify which
    occurrence a write targets. Creates carry no uid and are skipped with a
    falsy key (the truthy ``"::"`` shape would hit the fetch with no record to
    find), as are the two write shapes no snapshot could undo.
    """
    entity_id = kw.get("entity_id")
    uid = kw.get("uid")
    if not entity_id or not uid:
        return ""
    if kw.get("recurrence_range"):
        # A ranged write splits the series: Home Assistant truncates the
        # original at this occurrence and starts a second one under the same
        # uid. Restoring the captured occurrence would land on the NEW series
        # and fork that single date out of it, leaving every later date
        # edited while reporting success.
        # Debug, not warning: the decorator runs this key builder twice per
        # call, and once even when auto-backup is switched off.
        logger.debug(
            "Auto-backup: no snapshot for the ranged write on %s (event %s); "
            "a recurrence range rewrites the series, which a per-occurrence "
            "snapshot cannot undo",
            entity_id,
            uid,
        )
        return ""
    if kw.get("rrule"):
        # Turning an event into a series cannot be undone through the update
        # command: Home Assistant validates ``rrule`` with a rule parser that
        # rejects both null and empty, so the restore has no way to say
        # "no recurrence" and the stored rule survives the merge.
        logger.debug(
            "Auto-backup: no snapshot for the recurrence-rule write on %s "
            "(event %s); Home Assistant offers no way to clear a rule again",
            entity_id,
            uid,
        )
        return ""
    recurrence_id = kw.get("recurrence_id")
    if recurrence_id:
        return f"{entity_id}::{uid}::{recurrence_id}"
    return f"{entity_id}::{uid}"


def _validate_recurrence_target(
    entity_id: str,
    recurrence_id: str | None,
    recurrence_range: Literal["THISANDFUTURE"] | None,
) -> None:
    """Reject recurrence targeting that Home Assistant would silently widen.

    A range starts AT the identified occurrence, so Home Assistant ignores
    ``recurrence_range`` unless ``recurrence_id`` is set (ical gates the whole
    fork on it). Without the identifier an update then edits the master event
    and a delete removes the ENTIRE series rather than this occurrence and the
    following ones — both silently, and both reported as success.
    """
    if recurrence_id is not None:
        validate_identifier_not_empty(
            recurrence_id,
            "recurrence_id",
            suggestions=[
                (
                    "Use ha_config_get_calendar_events() to list occurrences "
                    "and obtain a valid recurrence_id"
                ),
                "Omit recurrence_id to target the whole series",
            ],
            context={"entity_id": entity_id},
        )
    if recurrence_range is not None and recurrence_range != _THIS_AND_FUTURE:
        # The ``Literal`` annotation only binds MCP calls, which the tool's
        # TypeAdapter validates; a direct Python call reaches here unchecked
        # and Home Assistant compares the value verbatim, so a near-miss
        # spelling silently degrades to a single-occurrence write.
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"recurrence_range must be {_THIS_AND_FUTURE!r}: Home Assistant "
                "compares the value verbatim",
                context={
                    "entity_id": entity_id,
                    "recurrence_range": recurrence_range,
                },
                suggestions=[
                    f"Pass {_THIS_AND_FUTURE!r} to target this and later occurrences",
                    "Omit recurrence_range to target a single occurrence",
                ],
            )
        )
    if recurrence_range is not None and recurrence_id is None:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "recurrence_range requires recurrence_id: the range starts at "
                "the identified occurrence",
                context={
                    "entity_id": entity_id,
                    "recurrence_range": recurrence_range,
                },
                suggestions=[
                    "Pass the recurrence_id of the occurrence the range starts at",
                    "Omit recurrence_range to target the whole series",
                    "Use ha_config_get_calendar_events() to list occurrences",
                ],
            )
        )


def is_date_only(value: str) -> bool:
    """Return whether value is a valid date in strict YYYY-MM-DD form."""
    try:
        return (
            len(value) == 10
            and datetime.strptime(value, "%Y-%m-%d").date().isoformat() == value
        )
    except ValueError:
        return False


def _recurrence_id_window(
    recurrence_id: str | None,
) -> tuple[datetime, datetime] | None:
    """A day-wide window around an iCalendar recurrence id, if it parses."""
    if not recurrence_id:
        return None
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M%SZ", "%Y%m%d"):
        try:
            at = datetime.strptime(recurrence_id, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
        return at - timedelta(days=1), at + timedelta(days=1)
    return None


def _calendar_bound(value: Any) -> tuple[str, bool] | None:
    """Return ``(iso_value, is_date_only)`` for a snapshotted event boundary.

    The REST view wraps each boundary as ``{"dateTime": ...}`` (timed) or
    ``{"date": ...}`` (all-day); snapshots taken before that switch hold a
    flat ISO string, which a 10-character length identifies as date-only.
    """
    if isinstance(value, dict):
        if value.get("dateTime"):
            return str(value["dateTime"]), False
        if value.get("date"):
            return str(value["date"]), True
        return None
    if isinstance(value, str) and value:
        return value, len(value) == 10
    return None
