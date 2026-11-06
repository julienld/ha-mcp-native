"""Exercise Core blueprint persistence, consumers, reloads, imports and restores."""

import json
import re
from typing import Any

from configuration_checks import expect_state
from dashboard_checks import _restore as restore_snapshot
from entity_checks import _backups as snapshot_rows


def _source(domain: str, *, changed: bool = False) -> str:
    action = "turn_off" if changed else "turn_on"
    version = "Changed" if changed else "Original"
    tail = (
        "triggers:\n  - trigger: event\n    event_type: native_blueprint_never_fired\nactions:\n"
        if domain == "automation"
        else "sequence:\n"
    )
    return (
        f"# Native installed {version}\nblueprint:\n  name: Native {domain} {version}\n"
        f"  description: Native {version}\n  domain: {domain}\n  input:\n"
        "    target_entity:\n      name: Target\n      selector:\n        entity: {}\n"
        + tail
        + f"  - action: input_boolean.{action}\n    target:\n      entity_id: !input target_entity\n"
    )


async def _failure(checks: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    response = await checks.rpc(
        "tools/call", {"name": "ha_manage_blueprints", "arguments": arguments}
    )
    assert "error" not in response and response["result"].get("isError"), response
    result = response["result"]
    return result.get("structuredContent") or json.loads(
        next(block["text"] for block in result["content"] if block["type"] == "text")
    )


async def exercise_blueprints(checks: Any, base: str, source_url: str) -> None:
    panel = base + "/api/ha_mcp_tools/ui/app/api/settings/backups"
    guide = await checks.call("ha_get_skill_guide")
    acknowledgment = re.search(r"Acknowledgment key: ([^\s]+)", guide["content"])
    assert acknowledgment, guide
    common = {"BestPracticeKey": acknowledgment[1], "MandatoryBPS": False}
    future_path = "native-fixture/future.yaml"
    future = _source("automation").replace(
        "  input:\n", "  homeassistant:\n    min_version: '9999.1.0'\n  input:\n", 1
    )
    refused = await _failure(
        checks, {"action": "save", "path": future_path, "yaml": future}
    )
    assert refused["error"]["code"] == "VALIDATION_FAILED", refused
    absent = await _failure(checks, {"action": "get", "path": future_path})
    assert absent["error"]["code"] == "RESOURCE_NOT_FOUND", absent
    checks.passed("native_blueprint_Core_compatibility_validation_prevents_persistence")
    for domain in ("automation", "script"):
        await _exercise_domain(checks, panel, domain, common)

    imported = await checks.call(
        "ha_manage_blueprints",
        {"action": "import", "url": source_url, "domain": "ignored"},
    )
    data = imported["data"]
    assert imported["success"] and not data["overrides_existing"], imported
    path = data["imported_blueprint"]["path"]
    read = await checks.call("ha_manage_blueprints", {"action": "get", "path": path})
    assert (
        read["data"]["yaml_source"] == "file"
        and read["data"]["metadata"]["source_url"] == source_url
    ), read
    repeated = await checks.call(
        "ha_manage_blueprints",
        {"action": "import", "url": source_url, "overwrite": True},
    )
    assert repeated["data"]["overrides_existing"], repeated
    await checks.call(
        "ha_manage_blueprints", {"action": "delete", "path": path, "confirm": True}
    )
    checks.passed(
        "native_blueprint_Core_importer_downloads_from_external_fixture_and_reimports"
    )


async def _exercise_domain(
    checks: Any, panel: str, domain: str, common: dict[str, Any]
) -> None:
    path = f"native-fixture/{domain}.yaml"
    args = {"domain": domain, "path": path}
    created = await checks.call(
        "ha_manage_blueprints",
        {
            **args,
            "action": "save",
            "path": path.removesuffix(".yaml"),
            "yaml": _source(domain),
        },
    )
    assert created["success"] and created["data"]["path"] == path, created
    read = await checks.call("ha_manage_blueprints", {**args, "action": "get"})
    initial = read["data"]["yaml"]
    assert read["data"]["yaml_source"] == "file" and "!input" in initial, read
    rendered = await checks.call(
        "ha_manage_blueprints",
        {
            **args,
            "action": "substitute",
            "input": {"target_entity": "input_boolean.native_light"},
        },
    )
    key = "actions" if domain == "automation" else "sequence"
    assert (
        rendered["data"]["config"][key][0]["target"]["entity_id"]
        == "input_boolean.native_light"
    ), rendered
    if domain == "script":
        await _create_consumer(checks, args, common)
    overwritten = await checks.call(
        "ha_manage_blueprints",
        {
            **args,
            "action": "save",
            "yaml": _source(domain, changed=True),
            "overwrite": True,
        },
    )
    assert overwritten["success"] and overwritten["data"]["overrides_existing"], (
        overwritten
    )
    if domain == "script":
        await _run_consumer(checks, common, "off", "overwritten")
    rows = await snapshot_rows(checks, panel, f"blueprint_{domain}", path)
    snapshot = rows[-1]["name"]
    stored = await checks.client.get(panel + "/" + snapshot)
    stored.raise_for_status()
    assert stored.json()["data"]["config"] == initial, stored.json()
    diff = await checks.client.get(panel + "/" + snapshot + "/diff")
    assert diff.status_code == 200 and "Changed" in diff.text, diff.text
    restored = await restore_snapshot(checks, panel, snapshot)
    assert restored["safety_backup"], restored
    if domain == "script":
        await _run_consumer(checks, common, "on", "restored")
        await checks.call(
            "ha_config_remove_script", {"script_id": "native_blueprint_consumer"}
        )
    await checks.call(
        "ha_manage_blueprints", {**args, "action": "delete", "confirm": True}
    )
    absent = await _failure(checks, {**args, "action": "get"})
    assert absent["error"]["code"] == "RESOURCE_NOT_FOUND", absent
    recreated = await restore_snapshot(checks, panel, snapshot)
    assert recreated["safety_backup"] is None, recreated
    await restore_snapshot(checks, panel, snapshot)
    listed = await checks.call(
        "ha_manage_blueprints", {"action": "list", "domain": domain}
    )
    assert sum(row["path"] == path for row in listed["data"]["blueprints"]) == 1, listed
    checks.passed(
        f"native_blueprint_{domain}_Core_CRUD_substitute_diff_restore_and_recreation"
    )


async def _create_consumer(
    checks: Any, args: dict[str, str], common: dict[str, Any]
) -> None:
    created = await checks.call(
        "ha_config_set_script",
        {
            **common,
            "script_id": "native_blueprint_consumer",
            "config": {
                "alias": "Native blueprint consumer",
                "use_blueprint": {
                    "path": args["path"],
                    "input": {"target_entity": "input_boolean.native_light"},
                },
            },
        },
    )
    assert created["success"], created
    read = await checks.call("ha_manage_blueprints", {**args, "action": "get"})
    assert "script.native_blueprint_consumer" in read["data"]["used_by"], read
    refused = await _failure(checks, {**args, "action": "delete", "confirm": True})
    assert (
        refused["error"]["code"] == "RESOURCE_LOCKED"
        and "script.native_blueprint_consumer" in refused["in_use_by"]
    ), refused
    await _run_consumer(checks, common, "on", "initial")


async def _run_consumer(
    checks: Any, common: dict[str, Any], expected: str, phase: str
) -> None:
    await checks.call(
        "ha_call_service",
        {
            "domain": "input_boolean",
            "service": "turn_off" if expected == "on" else "turn_on",
            "entity_id": "input_boolean.native_light",
        },
    )
    await checks.call(
        "ha_config_set_script",
        {**common, "script_id": "native_blueprint_consumer", "run": "start"},
    )
    await expect_state(checks, "input_boolean.native_light", expected)
    checks.passed(f"native_blueprint_consumer_{phase}_runs_current_{expected}_action")
