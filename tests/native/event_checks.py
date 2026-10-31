"""Qualify direct bus emission and actor context against the running Core."""

import asyncio
import time
from typing import Any


async def _events(checks: Any, count: int) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while True:
        observed: dict[str, Any] = await checks.control("event")
        if len(observed["events"]) == count:
            return observed
        if time.monotonic() >= deadline:
            raise AssertionError(observed)
        await asyncio.sleep(0.05)


async def exercise_events(checks: Any) -> None:
    payload = {"nested": {"value": 42}, "context": {"user_id": "forged"}}
    result = await checks.call(
        "ha_call_event",
        {"event_type": "native_fixture_custom_event", "data": payload},
    )
    assert result["success"] and result["message"].endswith("fired."), result
    observed = await _events(checks, 1)
    event = observed["events"][0]
    assert event["data"] == payload, observed
    assert event["user_id"] == observed["request_user_id"], observed
    assert event["user_id"] != "forged" and event["context_id"], observed
    first_context = event["context_id"]
    await checks.call("ha_call_event", {"event_type": "native_fixture_custom_event"})
    observed = await _events(checks, 2)
    assert observed["events"][1]["context_id"] != first_context, observed
    assert observed["events"][1]["data"] == {}, observed
    checks.passed("native_event_delivery_uses_real_Core_bus_and_fresh_actor_context")
