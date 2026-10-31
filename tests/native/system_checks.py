"""Verify live Core reloads; process restart is a separate end-of-run exercise."""

import asyncio
import time
from typing import Any


async def exercise_system_actions(checks: Any) -> None:
    seed = await checks.control("seed_system_actions", {})
    previous = await checks.call("ha_get_state", {"entity_id": seed["entity_id"]})
    assert previous.get("data", previous)["attributes"]["friendly_name"] == seed["name"]
    proposed = "Native lifecycle after reload"
    await checks.control("stage_system_script", {"name": proposed})
    unchanged = await checks.call("ha_get_state", {"entity_id": seed["entity_id"]})
    assert (
        unchanged.get("data", unchanged)["attributes"]["friendly_name"] == seed["name"]
    )
    reloaded = await checks.call("ha_reload_core", {"target": "scripts"})
    assert reloaded["success"] and reloaded["service"] == "script.reload", reloaded
    state = await checks.call("ha_get_state", {"entity_id": seed["entity_id"]})
    assert state.get("data", state)["attributes"]["friendly_name"] == proposed, state
    checks.passed("native_reload_script_applies_fresh_persisted_Core_configuration")

    before = await checks.control("system_actions")
    result = await checks.call("ha_reload_core", {"entry_id": seed["entry_id"]})
    assert result["reloaded"] and not result["require_restart"], result
    probe = await checks.control("system_actions")
    assert probe["entry_state"] == "loaded", probe
    assert (
        probe["entry_lifecycle"]["unload"] == before["entry_lifecycle"]["unload"] + 1
    ), probe
    assert (
        probe["entry_lifecycle"]["setup"] == before["entry_lifecycle"]["setup"] + 1
    ), probe
    checks.passed("native_reload_config_entry_uses_real_unload_and_setup")

    refused = await checks.rpc(
        "tools/call", {"name": "ha_restart", "arguments": {"confirm": False}}
    )
    assert refused["result"]["isError"], refused
    assert refused["result"]["_meta"]["ha_mcp"]["status"] == "not_started", refused
    assert not (await checks.control("system_actions"))["pending"]
    checks.passed("native_restart_requires_confirmation_without_scheduling")


async def exercise_system_self_reload(checks: Any) -> None:
    """Run near the end: the HTTP owner really unloads and is constructed anew."""
    before = await checks.control("system_actions")
    path = checks.path
    scheduled = await checks.call(
        "ha_reload_core", {"entry_id": before["manager_entry_id"]}
    )
    assert scheduled["scheduled"] and not scheduled["reloaded"], scheduled
    deadline = time.monotonic() + 90
    while True:
        probe = await checks.control("system_actions")
        if (
            probe["manager_identity"] is not None
            and probe["manager_identity"] != before["manager_identity"]
        ):
            break
        if time.monotonic() >= deadline:
            raise AssertionError({"before": before, "after": probe})
        await asyncio.sleep(0.2)
    ready = await checks.ready()
    assert ready["entry_id"] == before["manager_entry_id"] and checks.path == path, (
        ready
    )
    healthy = await checks.call(
        "ha_get_state", {"entity_id": "input_boolean.native_light"}
    )
    assert healthy.get("data", healthy)["entity_id"] == "input_boolean.native_light", (
        healthy
    )
    checks.passed(
        "native_self_reload_replies_then_replaces_its_HTTP_owner_without_a_drain_cycle"
    )
