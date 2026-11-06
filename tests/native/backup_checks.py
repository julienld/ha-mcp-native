"""Create two genuine local archives and preserve the newest-delete safeguard."""

import json
from typing import Any


async def exercise_backups(checks: Any) -> None:
    seeded = await checks.control("seed_backups", {})
    first = await checks.call(
        "ha_manage_backup",
        {
            "scope": "snapshot",
            "action": "create",
            "name": seeded["prefix"] + "first",
            "include_homeassistant": True,
            "include_database": False,
        },
    )
    first_id = first["backup_id"]
    first_probe = await checks.control("backup_probe", {"backup_id": first_id})
    assert first_probe["present"], first_probe
    assert first_probe["backup"]["agents"][seeded["agent_id"]]["size"] > 0, first_probe
    assert (
        first_probe["backup"]["homeassistant_included"]
        and not first_probe["backup"]["database_included"]
    ), first_probe
    assert first_probe["backup"]["extra_metadata"]["ha_mcp_creation_id"], first_probe
    checks.passed(
        "native_backup_creates_a_real_local_Core_archive_and_correlates_its_nonce"
    )
    try:
        rejected = await checks.rpc(
            "tools/call",
            {
                "name": "ha_manage_backup",
                "arguments": {
                    "scope": "snapshot",
                    "action": "delete",
                    "backup_id": first_id,
                    "confirm": True,
                },
            },
        )
        assert rejected["result"].get("isError"), rejected
        text = json.dumps(rejected).lower()
        assert "newest" in text, rejected
        second = await checks.call(
            "ha_manage_backup",
            {
                "scope": "snapshot",
                "action": "create",
                "name": seeded["prefix"] + "second",
                "include_homeassistant": True,
                "include_database": False,
            },
        )
        second_id = second["backup_id"]
        second_probe = await checks.control("backup_probe", {"backup_id": second_id})
        assert second_probe["present"] and first_id != second_id, second_probe
        listing = await checks.call(
            "ha_manage_backup", {"scope": "snapshot", "action": "list"}
        )
        assert {first_id, second_id} <= {
            row["backup_id"] for row in listing["backups"]
        }, listing
        deleted = await checks.call(
            "ha_manage_backup",
            {
                "scope": "snapshot",
                "action": "delete",
                "backup_id": first_id,
                "confirm": True,
            },
        )
        assert deleted["success"], deleted
        assert not (await checks.control("backup_probe", {"backup_id": first_id}))[
            "present"
        ]
        assert (await checks.control("backup_probe", {"backup_id": second_id}))[
            "present"
        ]
        checks.passed(
            "native_backup_lists_real_archives_refuses_newest_and_deletes_only_the_older_target"
        )
    finally:
        await checks.control("cleanup_backups", {})


async def _exercise_edit_backup(checks: Any, entity_id: str) -> None:
    inspected = await checks.control("entity")
    registry = inspected["registry_entry"]
    assert registry["entity_id"] == entity_id, inspected
    current = await checks.call("ha_get_entity", {"entity_id": entity_id})
    captured = await checks.call(
        "ha_manage_backup",
        {
            "scope": "edits",
            "action": "create",
            "domain": "entity",
            "entity_id": entity_id,
        },
    )
    name = captured["data"]["backup_name"]
    try:
        viewed = await checks.call(
            "ha_manage_backup",
            {"scope": "edits", "action": "view", "backup_name": name},
        )
        config = viewed["data"]["config"]
        assert config["entity_id"] == entity_id, viewed
        assert config["id"] == registry["id"], (viewed, inspected)
        for key in ("platform", "unique_id", "name", "icon", "options"):
            assert config[key] == current["entity_entry"][key], (key, viewed, current)
        listing = await checks.call(
            "ha_manage_backup",
            {
                "scope": "edits",
                "action": "list",
                "domain": "entity",
                "entity_id": entity_id,
            },
        )
        assert any(row["name"] == name for row in listing["data"]["backups"]), listing
        diff = await checks.call(
            "ha_manage_backup",
            {"scope": "edits", "action": "diff", "backup_name": name},
        )
        assert not diff["data"]["entity_missing"], diff
        assert diff["data"]["unchanged"], diff
        checks.passed(
            "native_backup_tool_creates_views_lists_and_diffs_a_real_Core_registry_snapshot"
        )
    finally:
        deleted = await checks.call(
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": name},
        )
        assert name in deleted["data"]["deleted"], deleted
