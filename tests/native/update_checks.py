"""Qualify UpdateEntity, Core service validation, real Repairs and uncertain dispatch."""

from typing import Any


async def exercise_updates(checks: Any) -> None:
    seed = await checks.control("seed_updates", {})
    normal = seed["entities"]["normal"]
    listing = await checks.call("ha_manage_updates")
    row = next(row for row in listing["updates"] if row["entity_id"] == normal)
    assert row["attributes"]["native_future_field"] == {"schema": "next"}, listing
    detail = await checks.call(
        "ha_manage_updates", {"action": "get", "entity_ids": normal}
    )
    assert detail["release_notes_source"] == "core", detail
    assert (
        detail["release_notes"]
        == "Actual native fixture provider release notes for 2.0"
    ), detail
    checks.passed(
        "native_updates_read_real_UpdateEntity_notes_and_preserve_provider_attributes"
    )

    skipped = await checks.call(
        "ha_manage_updates", {"action": "skip", "entity_ids": normal}
    )
    assert skipped["succeeded"] == 1, skipped
    read = await checks.call(
        "ha_manage_updates", {"action": "get", "entity_ids": normal}
    )
    assert read["skipped_version"] == "2.0", read
    cleared = await checks.call(
        "ha_manage_updates", {"action": "clear_skipped", "entity_ids": normal}
    )
    assert cleared["succeeded"] == 1, cleared
    read = await checks.call(
        "ha_manage_updates", {"action": "get", "entity_ids": normal}
    )
    assert read["skipped_version"] is None, read
    installed = await checks.call(
        "ha_manage_updates", {"action": "install", "entity_ids": normal, "backup": True}
    )
    assert installed["succeeded"] == 1, installed
    probe = await checks.control("update_probe")
    assert (
        len(probe["installs"]["normal"]) == 1
        and probe["installs"]["normal"][0]["backup"]
    ), probe
    assert probe["installs"]["normal"][0]["actor"] is not None, probe
    detail = await checks.call(
        "ha_manage_updates", {"action": "get", "entity_ids": normal}
    )
    assert detail["installed_version"] == "2.0", detail
    checks.passed("native_update_services_skip_clear_install_and_keep_actor_and_backup")

    item = {key: seed["issue"][key] for key in ("domain", "issue_id")}
    ignored = await checks.call(
        "ha_manage_updates", {"action": "ignore_repair", "repairs": [item]}
    )
    assert (
        ignored["succeeded"] == 1
        and (await checks.control("update_probe"))["issue"]["dismissed_version"]
        is not None
    ), ignored
    restored = await checks.call(
        "ha_manage_updates", {"action": "unignore_repair", "repairs": [item]}
    )
    assert (
        restored["succeeded"] == 1
        and (await checks.control("update_probe"))["issue"]["dismissed_version"] is None
    ), restored
    checks.passed("native_repairs_ignore_and_unignore_the_real_Core_issue_registry")

    unknown = await checks.rpc(
        "tools/call",
        {
            "name": "ha_manage_updates",
            "arguments": {
                "action": "install",
                "entity_ids": [seed["entities"]["uncertain"], seed["entities"]["tail"]],
            },
        },
    )
    result = unknown["result"]
    assert result["_meta"]["ha_mcp"]["status"] == "outcome_unknown", unknown
    assert result["structuredContent"]["results"][1]["not_attempted"], unknown
    probe = await checks.control("update_probe")
    assert len(probe["installs"]["uncertain"]) == 1 and not probe["installs"]["tail"], (
        probe
    )
    checks.passed(
        "native_update_after_effect_failure_is_not_replayed_and_stops_the_batch"
    )
