"""Qualify native bulk/status through Core services, state events and registries."""

from typing import Any


async def exercise_bulk(checks: Any) -> None:
    fixture = await checks.control("seed_bulk", {})
    a, b, uncertain = (fixture["entities"][key] for key in ("a", "b", "uncertain"))
    selector = {"domain": "native_fixture", "area_ids": [fixture["area_id"]]}
    preview = await checks.call(
        "ha_bulk_control", {"selector": selector, "action": "on", "dry_run": True}
    )
    assert set(preview["resolution"]["resolved_entity_ids"]) == {a, b}, preview
    assert preview["dry_run"] and not preview["dispatched"], preview
    checks.passed("native_bulk_preview_uses_actual_Core_area_entity_device_topology")

    applied = await checks.call(
        "ha_bulk_control", {"selector": selector, "action": "on"}
    )
    assert applied["successful_commands"] == 2 and applied["failed_commands"] == 0, (
        applied
    )
    assert all(row["confirmed"] for row in applied["results"]), applied
    identifiers = applied["operation_ids"]
    assert len(identifiers) == 2 and all(
        value.startswith("device-") for value in identifiers
    ), applied
    status = await checks.call(
        "ha_get_operation_status", {"operation_id": identifiers, "timeout_seconds": 0}
    )
    assert status["completed"] == 2 and status["all_complete"], status
    assert all(
        row["verification_method"] == "core_state_change"
        for row in status["detailed_results"]
    ), status
    checks.passed(
        "native_bulk_Core_events_confirm_owned_device_ids_without_another_listener"
    )

    denied = await checks.rpc(
        "tools/call",
        {
            "name": "ha_get_operation_status",
            "arguments": {"operation_id": identifiers[0], "timeout_seconds": 0},
        },
        user="beta",
    )
    assert denied["result"]["isError"], denied
    assert (
        "RESOURCE_NOT_FOUND" in str(denied)
        and a not in str(denied)
        and b not in str(denied)
    ), denied
    checks.passed("native_device_operation_history_is_private_to_its_actor")

    toggled = await checks.call(
        "ha_bulk_control", {"operations": [{"entity_id": a, "action": "toggle"}] * 2}
    )
    first, second = toggled["results"]
    assert first["final_state"] == "off" and second["final_state"] == "on", toggled
    assert second["transitions"][0]["old_state"]["state"] == "off", toggled
    assert toggled["execution_mode"] == "sequential", toggled
    checks.passed(
        "native_repeated_targets_get_separate_Core_preimages_and_confirmations"
    )

    raw = await checks.rpc(
        "tools/call",
        {
            "name": "ha_bulk_control",
            "arguments": {
                "parallel": False,
                "operations": [
                    {"entity_id": uncertain, "action": "on"},
                    {"entity_id": b, "action": "off"},
                ],
            },
        },
    )
    payload = raw["result"]["structuredContent"]
    assert raw["result"]["_meta"]["ha_mcp"]["status"] == "outcome_unknown", raw
    assert (
        payload["partial"]
        and payload["results"][0]["error"]["code"] == "OUTCOME_UNKNOWN"
    ), payload
    assert not payload["results"][1]["attempted"], payload
    state = await checks.call("ha_get_state", {"entity_id": uncertain})
    state = state.get("data", state)
    assert state["state"] == "on" and state["attributes"]["native_dispatches"] == 1, (
        state
    )
    state = await checks.call("ha_get_state", {"entity_id": b})
    assert state.get("data", state)["state"] == "on", state
    checks.passed(
        "native_bulk_after_effect_failure_is_unknown_stops_tail_and_is_not_replayed"
    )
