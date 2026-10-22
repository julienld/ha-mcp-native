"""Qualify direct CalendarEntity tools on Core's real Local Calendar provider."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any


async def exercise_calendars(checks: Any) -> None:
    created = await checks.call(
        "ha_set_integration",
        {"domain": "local_calendar", "config": {"calendar_name": "Native calendar"}},
    )
    assert created["success"], created
    entity_id = "calendar.native_calendar"
    deadline = time.monotonic() + 12
    while True:
        state = await checks.call("ha_get_state", {"entity_id": entity_id})
        if state.get("success"):
            break
        if time.monotonic() >= deadline:
            raise AssertionError(state)
        await asyncio.sleep(0.1)
    start = (datetime.now(UTC) + timedelta(days=3)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(hours=1)
    bounds = {
        "entity_id": entity_id,
        "start": (start - timedelta(days=1)).isoformat(),
        "end": (end + timedelta(days=1)).isoformat(),
    }
    event = {
        "entity_id": entity_id,
        "summary": "Native appointment",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "description": "Original description",
        "location": "Original location",
    }
    added = await checks.call("ha_config_set_calendar_event", event)
    assert added["success"], added
    read = await checks.call("ha_config_get_calendar_events", bounds)
    assert len(read["events"]) == 1, read
    original = read["events"][0]
    assert original["summary"] == "Native appointment" and original["uid"], read
    updated = await checks.call(
        "ha_config_set_calendar_event",
        {**event, "uid": original["uid"], "summary": "Updated appointment"},
    )
    assert updated["success"], updated
    read = await checks.call("ha_config_get_calendar_events", bounds)
    assert len(read["events"]) == 1 and read["events"][0]["uid"] == original["uid"], read
    assert read["events"][0]["summary"] == "Updated appointment", read
    removed = await checks.call(
        "ha_config_remove_calendar_event",
        {"entity_id": entity_id, "uid": original["uid"]},
    )
    assert removed["success"], removed
    assert (await checks.call("ha_config_get_calendar_events", bounds))["events"] == []
    checks.passed("canonical_CalendarEntity_create_read_update_delete_on_Local_Calendar")

    recurring = await checks.call(
        "ha_config_set_calendar_event", {**event, "rrule": "FREQ=DAILY;COUNT=3"}
    )
    assert recurring["success"], recurring
    read = await checks.call(
        "ha_config_get_calendar_events",
        {**bounds, "end": (end + timedelta(days=4)).isoformat()},
    )
    assert len(read["events"]) == 3, read
    occurrence = read["events"][1]
    assert occurrence["uid"] and occurrence["recurrence_id"], read
    deleted = await checks.call(
        "ha_config_remove_calendar_event",
        {
            "entity_id": entity_id,
            "uid": occurrence["uid"],
            "recurrence_id": occurrence["recurrence_id"],
            "recurrence_range": "THISANDFUTURE",
        },
    )
    assert deleted["success"] and deleted["warnings"], deleted
    read = await checks.call(
        "ha_config_get_calendar_events",
        {**bounds, "end": (end + timedelta(days=4)).isoformat()},
    )
    assert len(read["events"]) == 1, read
    checks.passed("canonical_calendar_recurrence_range_preserves_scope_and_backup_warning")
