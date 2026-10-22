"""Real component entry, filesystem producers, YAML previews and restore history."""

import asyncio
import json
import re
import time
from typing import Any

from dashboard_checks import _failure, _restore
from entity_checks import _backups


async def _tools_entry(checks: Any, base: str) -> None:
    url = base + "/api/config/config_entries/flow"
    response = await checks.client.post(
        url,
        headers=checks.headers(),
        json={"handler": "ha_mcp_tools", "show_advanced_options": False},
    )
    response.raise_for_status()
    flow = response.json()
    url += "/" + flow["flow_id"]
    assert flow["type"] == "menu", flow
    response = await checks.client.post(
        url, headers=checks.headers(), json={"next_step_id": "tools"}
    )
    response.raise_for_status()
    assert response.json()["step_id"] == "tools", response.text
    response = await checks.client.post(url, headers=checks.headers(), json={})
    response.raise_for_status()
    assert response.json()["type"] == "create_entry", response.text
    deadline = time.monotonic() + 30
    while not (await checks.control("file_probe"))["ready"]:
        assert time.monotonic() < deadline, "File & YAML entry did not load"
        await asyncio.sleep(0.1)


async def exercise_files_yaml(checks: Any, base: str) -> None:
    panel = base + "/api/ha_mcp_tools/ui/app/api/settings"
    response = await checks.client.post(
        base + "/api/ha_mcp_tools/ui/session", headers=checks.headers()
    )
    response.raise_for_status()
    response = await checks.client.post(
        panel + "/features",
        json={
            "flags": {
                "enable_beta_features": True,
                "enable_filesystem_tools": True,
                "enable_yaml_config_editing": True,
                "enable_yaml_edit_confirm": True,
            }
        },
    )
    assert response.status_code == 200, response.text
    refused = await _failure(checks, "ha_read_file", {"path": "configuration.yaml"})
    reason = refused.get("structuredContent") or json.loads(
        next(block["text"] for block in refused["content"] if block["type"] == "text")
    )
    assert reason["error"]["code"] == "COMPONENT_NOT_INSTALLED", reason
    await _tools_entry(checks, base)
    checks.passed("native_files_require_the_separate_real_Tools_config_entry")
    await _files(checks, panel)
    guide = await checks.call("ha_get_skill_guide")
    match = re.search(r"Acknowledgment key: ([^\s]+)", guide["content"])
    assert match, guide
    await _yaml(checks, panel, {"BestPracticeKey": match[1], "MandatoryBPS": False})
    assert (await checks.control("file_probe"))["pending"] == 0


async def _files(checks: Any, panel: str) -> None:
    path = "www/native-contract.txt"
    created = await checks.call("ha_write_file", {"path": path, "content": "before"})
    assert created["created"] and created["success"], created
    listed = await checks.call(
        "ha_list_files", {"path": "www", "pattern": "native-contract.txt"}
    )
    assert (
        listed["count"] == 1 and listed["files"][0]["name"] == "native-contract.txt"
    ), listed
    await checks.call(
        "ha_write_file", {"path": path, "content": "after", "overwrite": True}
    )
    rows = await _backups(checks, panel + "/backups", "file", path)
    restored = await _restore(checks, panel + "/backups", rows[0]["name"])
    assert restored["apply_status"] == "applied" and restored["safety_backup"], restored
    assert (await checks.call("ha_read_file", {"path": path}))["content"] == "before"
    await checks.call("ha_delete_file", {"path": path})
    await _restore(checks, panel + "/backups", rows[0]["name"])
    assert (await checks.call("ha_read_file", {"path": path}))["content"] == "before"
    await checks.call("ha_delete_file", {"path": path})
    checks.passed("native_file_CRUD_exact_backups_UI_restore_and_deleted_file_recovery")

    response = await checks.client.post(
        panel + "/fs-custom-paths", json={"paths": ["native-files", ".storage"]}
    )
    assert response.status_code == 200 and response.json()["rejected"] == [
        ".storage"
    ], response.text
    await checks.call(
        "ha_write_file", {"path": "native-files/note.txt", "content": "custom"}
    )
    await checks.call("ha_delete_file", {"path": "native-files/note.txt"})
    response = await checks.client.post(panel + "/fs-custom-paths", json={"paths": []})
    assert response.status_code == 200, response.text
    await _failure(
        checks,
        "ha_write_file",
        {"path": "native-files/refused.txt", "content": "denied"},
    )
    await _failure(checks, "ha_read_file", {"path": ".storage/auth"})
    checks.passed(
        "native_file_permission_store_hot_swap_and_non_overridable_deny_floor"
    )


async def _yaml(checks: Any, panel: str, common: dict[str, Any]) -> None:
    path = "packages/native_contract.yaml"
    args = {
        **common,
        "file": path,
        "yaml_path": "utility_meter",
        "action": "add",
        "content": "# preserved subtree\ninitial:\n  source: sensor.native_initial\n",
    }
    preview = await checks.call("ha_config_set_yaml", args)
    assert preview["preview"] and not preview["written"], preview
    created = await checks.call(
        "ha_config_set_yaml", {**args, "confirm_token": preview["confirm_token"]}
    )
    assert created["written"] and created["config_check"] == "ok", created
    before = await checks.call(
        "ha_config_get_yaml",
        {"file": path, "yaml_path": "utility_meter", "include_parsed": True},
    )
    assert (
        before["matches"][0]["parsed"]["initial"]["source"] == "sensor.native_initial"
    ), before
    args.update(
        action="replace", content="requested:\n  source: sensor.native_requested\n"
    )
    preview = await checks.call("ha_config_set_yaml", args)
    assert (await checks.control("file_external_edit", {}))["written"]
    stale = await checks.call(
        "ha_config_set_yaml", {**args, "confirm_token": preview["confirm_token"]}
    )
    assert (
        stale["preview"] and stale["confirm_token_mismatch"] and not stale["written"]
    ), stale
    unchanged = await checks.call(
        "ha_config_get_yaml", {"file": path, "yaml_path": "utility_meter"}
    )
    assert "native_external" in unchanged["matches"][0]["content"], unchanged
    written = await checks.call(
        "ha_config_set_yaml", {**args, "confirm_token": stale["confirm_token"]}
    )
    assert written["written"], written
    rows = await _backups(checks, panel + "/backups", "yaml", path + "::utility_meter")
    diff = await checks.client.get(panel + "/backups/" + rows[0]["name"] + "/diff")
    assert (
        diff.status_code == 200
        and "native_external" in diff.text
        and "native_requested" in diff.text
    ), diff.text
    await _restore(checks, panel + "/backups", rows[0]["name"])
    restored = await checks.call(
        "ha_config_get_yaml",
        {"file": path, "yaml_path": "utility_meter", "include_parsed": True},
    )
    assert (
        restored["matches"][0]["parsed"]["external"]["source"]
        == "sensor.native_external"
    ), restored
    checks.passed(
        "native_YAML_preimage_confirmation_preserves_external_edit_and_subtree_safety_restore"
    )
