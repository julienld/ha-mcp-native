"""Qualify dashboard edits and restoration through the actual Lovelace owners."""

import asyncio
import json
import re
import time
from typing import Any


async def _failure(checks: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = await checks.rpc("tools/call", {"name": name, "arguments": arguments})
    assert "error" not in response, response
    result = response["result"]
    assert result.get("isError"), result
    return result


async def _snapshots(checks: Any, panel: str, target: str) -> list[dict[str, Any]]:
    response = await checks.client.get(
        panel, params={"domain": "dashboard", "entity_id": target}
    )
    response.raise_for_status()
    rows = response.json()["backups"]
    assert rows, response.json()
    return rows


async def _restore(checks: Any, panel: str, name: str) -> dict[str, Any]:
    response = await checks.client.post(panel + "/" + name + "/restore", json={})
    assert response.status_code == 200 and response.json()["success"], response.text
    return response.json()["data"]


async def _describe(checks: Any, card_type: str | None) -> dict[str, Any]:
    deadline = time.monotonic() + 90
    while True:
        response = await checks.rpc(
            "tools/call",
            {
                "name": "ha_config_get_dashboard",
                "arguments": {"describe": True, "card_type": card_type},
            },
        )
        assert "error" not in response, response
        result = response["result"]
        data = result.get("structuredContent")
        if data is None:
            data = json.loads(
                next(
                    item["text"] for item in result["content"] if item["type"] == "text"
                )
            )
        if not result.get("isError"):
            return data
        assert "still indexing" in str(data) or "still loading" in str(data), data
        if time.monotonic() >= deadline:
            raise AssertionError(data)
        await asyncio.sleep(0.5)


async def exercise_dashboards(checks: Any, base: str) -> None:
    """Run before resources' first list, preserving the real unloaded state."""
    panel = base + "/api/ha_mcp_tools/ui/app/api/settings/backups"
    session = await checks.client.post(
        base + "/api/ha_mcp_tools/ui/session", headers=checks.headers()
    )
    session.raise_for_status()
    guide = await checks.call("ha_get_skill_guide")
    acknowledgment = re.search(r"Acknowledgment key: ([^\s]+)", guide["content"])
    assert acknowledgment, guide
    common = {"BestPracticeKey": acknowledgment[1], "MandatoryBPS": False}
    before = await checks.control("dashboard_probe")
    assert before["resources_loaded"] is False, before
    default = await checks.control("seed_default_dashboard", {})
    for alias in ("default", "lovelace"):
        read = await checks.call("ha_config_get_dashboard", {"url_path": alias})
        assert read["config"] == default["config"], read
    checks.passed("native_dashboard_default_aliases_resolve_actual_None_owner")

    target = "native-contract-dashboard"
    original = {
        "views": [
            {
                "path": "home",
                "title": "Native before",
                "cards": [
                    {
                        "type": "custom:native-unavailable",
                        "entity": "sensor.native_energy",
                    }
                ],
            }
        ]
    }
    created = await checks.call(
        "ha_config_set_dashboard",
        {**common, "url_path": target, "title": "Native dashboard", "config": original},
    )
    assert created["success"] and created["post_write_verified"], created
    assert any("resources are not loaded" in note for note in created["warnings"]), (
        created
    )
    probe = await checks.control("dashboard_probe")
    assert probe["resources_loaded"] is False, probe
    row = next(row for row in probe["dashboards"] if row["key"] == target)
    assert row["owner_type"] == "LovelaceStorage" and row["config"] == original, row
    checks.passed("native_dashboard_postwrite_advice_does_not_initialize_resources")

    first = await checks.call("ha_config_get_dashboard", {"url_path": target})
    patched = await checks.call(
        "ha_config_set_dashboard",
        {
            **common,
            "url_path": target,
            "config_hash": first["config_hash"],
            "patch": [
                {"op": "replace", "path": "/views/0/title", "value": "Native patch"}
            ],
        },
    )
    assert patched["success"] and patched["config_hash"] != first["config_hash"], (
        patched
    )
    conflict = await _failure(
        checks,
        "ha_config_set_dashboard",
        {
            **common,
            "url_path": target,
            "title": "Must not apply",
            "config": {"views": []},
            "config_hash": first["config_hash"],
        },
    )
    assert "hash" in str(conflict).lower(), conflict
    transformed = await checks.call(
        "ha_config_set_dashboard",
        {
            **common,
            "url_path": target,
            "config_hash": patched["config_hash"],
            "python_transform": "config['views'][0]['title'] = 'Native transform'",
        },
    )
    assert transformed["success"] and transformed["post_write_verified"], transformed
    scoped = await checks.call(
        "ha_config_get_dashboard", {"url_path": target, "view_path": "home"}
    )
    assert scoped["view"]["title"] == "Native transform" and "config" not in scoped, (
        scoped
    )
    checks.passed("native_dashboard_Core_save_patch_hash_conflict_and_python_transform")

    await _exercise_restore(checks, panel, target, original, first["config_hash"])

    empty = "native-empty-dashboard"
    await checks.call("ha_config_set_dashboard", {**common, "url_path": empty})
    await checks.call(
        "ha_config_set_dashboard",
        {**common, "url_path": empty, "config": {"views": []}},
    )
    absent_snapshot = (await _snapshots(checks, panel, empty))[0]["name"]
    await _restore(checks, panel, absent_snapshot)
    probe = await checks.control("dashboard_probe")
    cleared = next(row for row in probe["dashboards"] if row["key"] == empty)
    assert cleared["config"] is None and cleared["metadata"]["url_path"] == empty, (
        cleared
    )
    checks.passed("native_dashboard_restore_absent_body_uses_Core_reset")

    described = await _describe(checks, "markdown")
    assert described["success"] and described["card_type"] == "markdown", described
    assert any(field["name"] == "content" for field in described["fields"]), described
    probe = await checks.control("dashboard_probe")
    assert probe["resources_loaded"] is False, probe
    checks.passed(
        "native_dashboard_builtin_description_uses_installed_frontend_without_migration"
    )


async def _exercise_restore(
    checks: Any,
    panel: str,
    target: str,
    original: dict[str, Any],
    original_hash: str,
) -> None:
    snapshots = await _snapshots(checks, panel, target)
    # Automatic captures can be throttled. Locate the exact original image rather
    # than relying on a timing-dependent number or order of subsequent captures.
    snapshot = None
    for entry in snapshots:
        response = await checks.client.get(panel + "/" + entry["name"])
        response.raise_for_status()
        if "Native before" in response.text:
            snapshot = entry["name"]
            break
    assert snapshot is not None, snapshots
    diff = await checks.client.get(panel + "/" + snapshot + "/diff")
    assert diff.status_code == 200 and "Native transform" in diff.text, diff.text
    restored = await _restore(checks, panel, snapshot)
    assert restored["safety_backup"], restored
    read = await checks.call("ha_config_get_dashboard", {"url_path": target})
    assert read["config"] == original and read["config_hash"] == original_hash, read
    deleted = await checks.call("ha_config_delete_dashboard", {"url_path": target})
    assert deleted["success"], deleted
    for _ in range(2):
        await _restore(checks, panel, snapshot)
    listed = await checks.call("ha_config_get_dashboard", {"list_only": True})
    matches = [row for row in listed["dashboards"] if row["url_path"] == target]
    assert len(matches) == 1 and matches[0]["title"] == "Native dashboard", listed
    read = await checks.call("ha_config_get_dashboard", {"url_path": target})
    assert read["config"] == original, read
    checks.passed("native_dashboard_UI_diff_restore_safety_and_repeat_recreation")


async def exercise_dashboard_card_types(checks: Any) -> None:
    """Run after resource migration/CRUD to inspect the live combined catalog."""
    described = await _describe(checks, None)
    assert described["success"] and "markdown" in described["card_types"], described
    assert "custom_status" in described, described
    checks.passed(
        "native_dashboard_card_types_use_real_frontend_and_resource_producers"
    )
