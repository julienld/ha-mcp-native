"""Live authoring and administrator restore checks for the disposable Core."""

from __future__ import annotations

import asyncio
import time
from typing import Any


async def expect_state(checks: Any, entity_id: str, expected: str) -> None:
    deadline = time.monotonic() + 12
    while True:
        state = await checks.call("ha_get_state", {"entity_id": entity_id})
        if state.get("state") == expected:
            return
        # ha_get_state exposes the single-state body under data on older clients.
        if state.get("data", {}).get("state") == expected:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                {"entity_id": entity_id, "expected": expected, "actual": state}
            )
        await asyncio.sleep(0.1)


async def exercise_configuration(
    checks: Any, common: dict[str, Any], base: str
) -> None:
    sequence = [
        {
            "variables": {
                "native_duration": "{{ timedelta(seconds=3) }}",
                "native_date": "{{ now().date() }}",
            }
        },
        {
            "action": "input_boolean.turn_on",
            "target": {"entity_id": "input_boolean.native_light"},
        },
    ]
    created = await checks.call(
        "ha_config_set_script",
        {
            **common,
            "script_id": "native_contract",
            "config": {"alias": "Native contract", "sequence": sequence},
        },
    )
    assert created["success"], created
    initial = await checks.call(
        "ha_config_get_script", {"script_id": "native_contract"}
    )
    assert initial["config"]["sequence"] == sequence, initial
    changed = await checks.call(
        "ha_config_set_script",
        {
            **common,
            "script_id": "native_contract",
            "config_hash": initial["config_hash"],
            "python_transform": "config['description'] = 'Native transform'",
        },
    )
    assert changed["success"], changed
    reread = await checks.call("ha_config_get_script", {"script_id": "native_contract"})
    assert reread["config"]["description"] == "Native transform", reread
    await checks.call(
        "ha_call_service",
        {
            "domain": "input_boolean",
            "service": "turn_off",
            "entity_id": "input_boolean.native_light",
        },
    )
    await checks.call(
        "ha_config_set_script",
        {**common, "script_id": "native_contract", "run": "start"},
    )
    await expect_state(checks, "input_boolean.native_light", "on")
    checks.passed("native_script_authoring_hash_transform_and_real_execution")
    await exercise_traces(checks)

    scene = await checks.call(
        "ha_config_set_scene",
        {
            **common,
            "scene_id": "native_contract",
            "config": {
                "name": "Native contract scene",
                "entities": {"input_boolean.native_light": "off"},
            },
            "activate": True,
        },
    )
    assert scene["success"] and scene.get("activated") is True, scene
    await expect_state(checks, "input_boolean.native_light", "off")
    checks.passed("native_scene_authoring_correlated_reload_before_activation")

    automation = await checks.call(
        "ha_config_set_automation",
        {
            **common,
            "config": {
                "alias": "Native contract automation",
                "triggers": [
                    {"trigger": "event", "event_type": "native_contract_unused"}
                ],
                "actions": sequence,
            },
            "run_actions": True,
        },
    )
    assert automation["success"] and automation.get("actions_triggered") is True, (
        automation
    )
    await expect_state(checks, "input_boolean.native_light", "on")
    identifier = automation["automation_id"]
    read = await checks.call("ha_config_get_automation", {"identifier": identifier})
    assert read["config"]["alias"] == "Native contract automation", read
    checks.passed("native_automation_authoring_and_correlated_reload_before_run")

    await exercise_snapshot_panel(checks, common, base, initial)

    for tool, arguments in (
        ("ha_config_remove_script", {"script_id": "native_contract"}),
        ("ha_config_remove_scene", {"scene_id": "native_contract"}),
        ("ha_config_remove_automation", {"identifier": identifier}),
    ):
        deleted = await checks.call(tool, arguments)
        assert deleted["success"], deleted
    checks.passed("native_configuration_deletions_through_Core_owners")


async def exercise_traces(checks: Any) -> None:
    deadline = time.monotonic() + 6
    while True:
        traces = await checks.call(
            "ha_get_automation_traces", {"automation_id": "script.native_contract"}
        )
        if traces["traces"]:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(traces)
        await asyncio.sleep(0.1)
    run_id = traces["traces"][0]["run_id"]
    detail = await checks.call(
        "ha_get_automation_traces",
        {"automation_id": "script.native_contract", "run_id": run_id, "detailed": True},
    )
    assert detail["success"] and detail["run_id"] == run_id, detail
    assert detail.get("action_trace"), detail
    checks.passed("native_trace_list_and_detail_use_Core_serialization_and_live_script")


async def exercise_snapshot_panel(
    checks: Any, common: dict[str, Any], base: str, initial: dict[str, Any]
) -> None:
    session = await checks.client.post(
        base + "/api/ha_mcp_tools/ui/session", headers=checks.headers()
    )
    session.raise_for_status()
    panel = base + "/api/ha_mcp_tools/ui/app/api/settings/backups"
    response = await checks.client.get(
        panel, params={"domain": "script", "entity_id": "native_contract"}
    )
    response.raise_for_status()
    backups = response.json()["backups"]
    assert backups, response.json()
    name = backups[0]["name"]
    diff = await checks.client.get(panel + "/" + name + "/diff")
    assert diff.status_code == 200, diff.text
    assert "Native transform" in diff.text, diff.json()
    restored = await checks.client.post(panel + "/" + name + "/restore", json={})
    assert restored.status_code == 200, restored.text
    assert restored.json()["success"], restored.json()
    reread = await checks.call("ha_config_get_script", {"script_id": "native_contract"})
    assert reread["config"] == initial["config"], reread
    # Strict BPS remains active on the following MCP invocation after an admin restore.
    assert common["BestPracticeKey"]
    checks.passed("native_admin_cookie_snapshot_diff_and_restore_with_safety_capture")


async def exercise_flow_helpers(checks: Any, common: dict[str, Any]) -> None:
    created = await checks.call(
        "ha_config_set_helper",
        {
            **common,
            "helper_type": "template",
            "name": "Native template",
            "config": {"next_step_id": "sensor", "state": "{{ 2 + 3 }}"},
        },
    )
    assert created["success"] and created["entity_ids"], created
    await expect_state(checks, created["entity_ids"][0], "5")
    updated = await checks.call(
        "ha_config_set_helper",
        {
            **common,
            "helper_type": "template",
            "helper_id": created["entry_id"],
            "config": {"state": "{{ 3 + 4 }}"},
        },
    )
    assert updated["success"], updated
    await expect_state(checks, created["entity_ids"][0], "7")
    checks.passed("native_menu_template_flow_and_options_update")


async def exercise_todo(checks: Any, base: str) -> None:
    created = await checks.call(
        "ha_set_integration",
        {"domain": "local_todo", "config": {"todo_list_name": "Native todo"}},
    )
    assert created["success"], created
    deadline = time.monotonic() + 12
    while True:
        lists = await checks.call("ha_get_todo")
        matches = [
            row for row in lists["todo_lists"] if row["friendly_name"] == "Native todo"
        ]
        if matches:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(lists)
        await asyncio.sleep(0.1)
    entity_id = matches[0]["entity_id"]
    added = await checks.call(
        "ha_set_todo_item",
        {"entity_id": entity_id, "summary": "Native item", "due_date": "2026-10-25"},
    )
    assert added["success"], added
    read = await checks.call("ha_get_todo", {"entity_id": entity_id})
    item = read["items"][0]
    assert item["summary"] == "Native item" and item["due"] == "2026-10-25", read
    updated = await checks.call(
        "ha_set_todo_item",
        {"entity_id": entity_id, "item": item["uid"], "status": "completed"},
    )
    assert updated["success"], updated
    deleted = await checks.call(
        "ha_remove_todo_item", {"entity_id": entity_id, "item": item["uid"]}
    )
    assert deleted["success"], deleted
    assert (await checks.call("ha_get_todo", {"entity_id": entity_id}))["items"] == []
    checks.passed("native_integration_creation_and_todo_CRUD_on_real_Core")

    panel = base + "/api/ha_mcp_tools/ui/app/api/settings/backups"
    response = await checks.client.get(
        panel,
        params={"domain": "todo_item", "entity_id": entity_id + "::" + item["uid"]},
    )
    response.raise_for_status()
    backups = response.json()["backups"]
    assert backups, response.json()
    # Latest is the completed item captured immediately before deletion.
    name = backups[0]["name"]
    for _ in range(2):
        restored = await checks.client.post(panel + "/" + name + "/restore", json={})
        assert restored.status_code == 200, restored.text
        assert restored.json()["success"], restored.json()
    read = await checks.call("ha_get_todo", {"entity_id": entity_id})
    assert len(read["items"]) == 1 and read["items"][0]["status"] == "completed", read
    checks.passed(
        "native_todo_recreation_restores_completion_without_duplicate_on_repeat"
    )
