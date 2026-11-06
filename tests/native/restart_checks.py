"""Qualify the deferred Core restart using the same disposable Container."""

import asyncio
import json
import time
from typing import Any

import httpx


async def exercise_process_restart(checks: Any, docker: Any, container: str) -> None:
    before = await checks.ready()
    path = checks.path
    started = json.loads(docker("inspect", container).stdout)[0]["State"]["StartedAt"]
    scheduled = await checks.call("ha_restart", {"confirm": True})
    assert scheduled["scheduled"] and scheduled["requested"], scheduled
    checks.passed("native_restart_replies_scheduled_before_Core_process_exit")
    deadline = time.monotonic() + 120
    while True:
        state = json.loads(docker("inspect", container).stdout)[0]["State"]
        if not state["Running"]:
            break
        assert time.monotonic() < deadline, (
            "The deferred Core restart did not stop the process"
        )
        await asyncio.sleep(0.25)
    assert state["ExitCode"] == 100, state
    docker("start", container)
    deadline = time.monotonic() + 150
    while True:
        try:
            after = await checks.ready()
            break
        except (
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.ReadError,
            httpx.WriteError,
        ):
            # A retained connection can reset while this idempotent GET is sent.
            pass
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in {404, 503}:
                raise
        assert time.monotonic() < deadline, "Core did not start after the restart exit"
        await asyncio.sleep(0.5)
    fresh = json.loads(docker("inspect", container).stdout)[0]["State"]["StartedAt"]
    assert (
        fresh != started
        and after["entry_id"] == before["entry_id"]
        and checks.path == path
    )
    await checks.control("guard", {})
    checks.passed("native_restart_preserves_the_actual_Core_entry_URL_and_actor_token")
    state = await checks.call("ha_get_state", {"entity_id": "sensor.native_fixture"})
    assert state["data"]["state"] == "ready", state
    imported = await checks.call(
        "ha_native_llm_call",
        {"name": "native_fixture/NativeEcho", "arguments": {"text": "after restart"}},
    )
    assert "AFTER RESTART" in json.dumps(imported), imported
    exported = await checks.control(
        "llm",
        {
            "tool_name": "ha_get_state",
            "arguments": {"entity_id": "sensor.native_fixture"},
        },
    )
    assert not exported["error"] and "ready" in json.dumps(exported), exported
    checks.passed(
        "native_restart_registers_both_LLM_API_directions_in_the_fresh_process"
    )
