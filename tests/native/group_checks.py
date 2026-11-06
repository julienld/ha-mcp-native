"""Qualify Core groups, aggregation and real snapshot recovery without RPC I/O."""

from typing import Any


async def exercise_groups(checks: Any, base: str, common: dict[str, Any]) -> None:
    helper = await checks.call(
        "ha_config_set_helper",
        {
            **common,
            "helper_type": "input_boolean",
            "name": "Native group second member",
            "config": {"initial": False},
        },
    )
    assert helper["success"] and helper["entity_id"], helper
    members = ["input_boolean.native_light", helper["entity_id"]]
    await checks.call(
        "ha_call_service",
        {"domain": "input_boolean", "service": "turn_on", "entity_id": members[0]},
    )
    target = "native_contract_group"
    created = await checks.call(
        "ha_config_set_group",
        {"object_id": target, "entities": members, "name": "Native group before"},
    )
    assert created["success"], created
    before = await checks.control("group_probe")
    assert before["owner"]["type"] == "Group" and before["state"] == "on", before
    assert before["owner"]["all"] is False and before["owner"]["icon"] is None, before
    assert before["user_id"] == before["request_user_id"], before
    changed = await checks.call(
        "ha_config_set_group",
        {
            "object_id": target,
            "all_on": True,
            "name": "Native group after",
            "icon": "mdi:home-group",
        },
    )
    assert changed["success"], changed
    after = await checks.control("group_probe")
    assert after["owner"]["all"] is True and after["state"] == "off", after
    assert after["user_id"] == after["request_user_id"], after
    listed = await checks.call("ha_config_list_groups")
    row = next(row for row in listed["groups"] if row["object_id"] == target)
    assert row["all"] is True and set(row["entity_ids"]) == set(members), listed
    checks.passed(
        "native_groups_use_Core_factory_callbacks_and_actual_aggregation_mode"
    )

    panel = base + "/api/ha_mcp_tools/ui/app/api/settings/backups"
    session = await checks.client.post(
        base + "/api/ha_mcp_tools/ui/session", headers=checks.headers()
    )
    session.raise_for_status()
    response = await checks.client.get(
        panel, params={"domain": "group", "entity_id": target}
    )
    response.raise_for_status()
    snapshots = response.json()["backups"]
    assert snapshots, response.json()
    saved = snapshots[0]["name"]
    diff = await checks.client.get(panel + "/" + saved + "/diff")
    assert diff.status_code == 200 and "Native group after" in diff.text, diff.text
    restored = await _restore_group(checks, panel, saved)
    assert restored["safety_backup"] and restored["runtime_only"], restored
    after = await checks.control("group_probe")
    assert after["owner"] == before["owner"] and after["state"] == "on", after
    assert after["user_id"] == after["request_user_id"], after
    checks.passed("native_group_UI_diff_restore_safety_and_nullable_icon_clear")

    await checks.call("ha_config_remove_group", {"object_id": target})
    missing = await checks.control("group_probe")
    assert missing["owner"] is None and missing["state"] is None, missing
    for _ in range(2):
        await _restore_group(checks, panel, saved)
    listed = await checks.call("ha_config_list_groups")
    assert sum(row["object_id"] == target for row in listed["groups"]) == 1, listed
    after = await checks.control("group_probe")
    assert after["owner"] == before["owner"], after
    checks.passed("native_group_remove_and_repeat_recreation_keep_one_Core_owner")


async def _restore_group(checks: Any, panel: str, name: str) -> dict[str, Any]:
    response = await checks.client.post(panel + "/" + name + "/restore", json={})
    assert response.status_code == 200 and response.json()["success"], response.text
    result: dict[str, Any] = response.json()["data"]
    return result
