"""Qualify real Lovelace migration, resource CRUD and safe recreation in Core."""

from typing import Any


async def exercise_resources(checks: Any, base: str) -> None:
    listed = await checks.call("ha_config_list_dashboard_resources")
    assert any(
        row["url"] == "/local/native-legacy.js" for row in listed["resources"]
    ), listed
    checks.passed("native_resource_initial_read_runs_Core_legacy_migration")
    original = ":root { --native-contract: blue; }"
    created = await checks.call(
        "ha_config_set_dashboard_resource",
        {"content": original, "resource_type": "css"},
    )
    assert created["success"] and created["resource_id"], created
    identifier = created["resource_id"]
    updated = await checks.call(
        "ha_config_set_dashboard_resource",
        {
            "resource_id": identifier,
            "content": ":root { --native-contract: red; }",
            "resource_type": "css",
        },
    )
    assert updated["success"], updated
    listed = await checks.call(
        "ha_config_list_dashboard_resources", {"include_content": True}
    )
    row = next(row for row in listed["resources"] if row["id"] == identifier)
    assert "red" in row["_content"], listed
    panel = base + "/api/ha_mcp_tools/ui/app/api/settings/backups"
    response = await checks.client.get(
        panel, params={"domain": "dashboard_resource", "entity_id": identifier}
    )
    response.raise_for_status()
    backups = response.json()["backups"]
    assert backups, response.json()
    snapshot = backups[0]["name"]
    restored = await checks.client.post(panel + "/" + snapshot + "/restore", json={})
    assert restored.status_code == 200 and restored.json()["success"], restored.text
    listed = await checks.call(
        "ha_config_list_dashboard_resources", {"include_content": True}
    )
    row = next(row for row in listed["resources"] if row["id"] == identifier)
    assert row["_content"] == original, listed
    checks.passed(
        "native_Lovelace_resource_create_update_and_restore_use_Core_collection"
    )
    deleted = await checks.call(
        "ha_config_delete_dashboard_resource", {"resource_id": identifier}
    )
    assert deleted["success"], deleted
    for _ in range(2):
        restored = await checks.client.post(
            panel + "/" + snapshot + "/restore", json={}
        )
        assert restored.status_code == 200 and restored.json()["success"], restored.text
    listed = await checks.call(
        "ha_config_list_dashboard_resources", {"include_content": True}
    )
    matches = [row for row in listed["resources"] if row.get("_content") == original]
    assert len(matches) == 1 and matches[0]["id"] != identifier, listed
    checks.passed(
        "native_resource_recreation_repeated_restore_does_not_duplicate_content"
    )
