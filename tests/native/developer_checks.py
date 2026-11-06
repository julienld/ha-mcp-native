"""Real native settings, human controls, policy CAS and retained report evidence."""

import json
from typing import Any

from dashboard_checks import _failure


async def exercise_developer_tools(checks: Any, base: str) -> None:
    root = base + "/api/ha_mcp_tools/ui/app/api/"
    session = await checks.client.post(
        base + "/api/ha_mcp_tools/ui/session", headers=checks.headers()
    )
    session.raise_for_status()
    features = await checks.client.get(root + "settings/features")
    features.raise_for_status()
    old_features = features.json()["flags"]
    advanced = await checks.client.get(root + "settings/advanced")
    advanced.raise_for_status()
    old_advanced = {row["field"]: row["value"] for row in advanced.json()["fields"]}
    before_tools = await checks.client.get(root + "settings/tools")
    before_tools.raise_for_status()
    before_policy = await checks.client.get(root + "policy/config")
    before_policy.raise_for_status()
    original = before_policy.json()
    enabled = await checks.client.post(
        root + "settings/advanced",
        json={"enable_dev_mode": True, "dev_tools_security_policy_access": False},
    )
    assert enabled.status_code == 200, enabled.text
    enabled = await checks.client.post(
        root + "settings/features",
        json={
            "flags": {
                "enable_security_policy_tool": True,
                "enable_tool_security_policies": True,
            }
        },
    )
    assert enabled.status_code == 200, enabled.text
    try:
        await _exercise_settings(checks, root)
        await _exercise_policy(checks, root)
        await _exercise_reports(checks, root)
    finally:
        current = await checks.client.get(root + "policy/config")
        current.raise_for_status()
        restored = await checks.client.put(
            root + "policy/config",
            json={**original, "version": current.json()["version"]},
        )
        assert restored.status_code == 200, restored.text
        restored = await checks.client.post(
            root + "settings/tools",
            json={
                "states": before_tools.json()["states"],
                "llm_api": before_tools.json()["llm_api_overrides"],
            },
        )
        assert restored.status_code == 200, restored.text
        restored = await checks.client.post(
            root + "settings/features",
            json={
                "flags": {
                    name: old_features[name]["value"]
                    for name in (
                        "enable_security_policy_tool",
                        "enable_tool_security_policies",
                    )
                }
            },
        )
        assert restored.status_code == 200, restored.text
        restored = await checks.client.post(
            root + "settings/advanced",
            json={
                name: old_advanced[name]
                for name in ("enable_dev_mode", "dev_tools_security_policy_access")
            },
        )
        assert restored.status_code == 200, restored.text


async def _exercise_settings(checks: Any, root: str) -> None:
    info = await checks.call("ha_dev_manage_server", {"action": "info"})
    assert (
        info["data"]["runtime"] == "native"
        and not info["data"]["update_source_supported"]
    ), info
    await _failure(
        checks,
        "ha_dev_manage_server",
        {"action": "update_source", "pip_spec": "ha-mcp==1.0"},
    )
    matrix = await checks.call("ha_dev_manage_settings", {"action": "list"})
    rows = {row["setting"]: row for row in matrix["data"]["settings"]}
    assert not rows["dev_tools_security_policy_access"]["editable"]
    await _failure(
        checks,
        "ha_dev_manage_settings",
        {
            "action": "set",
            "setting": "dev_tools_security_policy_access",
            "value": True,
        },
    )
    await _failure(
        checks,
        "ha_dev_manage_settings",
        {
            "action": "set_backup_config",
            "backup": {"snapshot_delete_min_age_days": 0},
        },
    )
    changed = await checks.call(
        "ha_dev_manage_settings",
        {
            "action": "set_tool",
            "tool": "ha_list_services",
            "state": "pinned",
            "llm_api": False,
        },
    )
    assert changed["data"]["llm_api"] is False, changed
    listed = await checks.call("ha_dev_manage_settings", {"action": "list_tools"})
    row = next(
        item for item in listed["data"]["tools"] if item["name"] == "ha_list_services"
    )
    assert row["state"] == "pinned" and row["llm_api"] is False, row
    checks.passed(
        "native_developer_real_settings_patch_and_human_controls_no_pip_install"
    )


async def _exercise_policy(checks: Any, root: str) -> None:
    policy = await checks.call("ha_manage_security_policy", {"action": "get"})
    document = policy["data"]["policy"]
    stored = await checks.call(
        "ha_manage_security_policy",
        {
            "action": "set",
            "policy": {
                **document,
                "rule_effect": "require_approval",
                "wait_seconds": 5,
                "rules": [{"tool_name": "ha_get_state"}],
            },
            "expected_version": document["version"],
        },
    )
    assert stored["data"]["version"] == document["version"] + 1, stored
    await _failure(
        checks,
        "ha_manage_security_policy",
        {
            "action": "set",
            "policy": document,
            "expected_version": document["version"],
        },
    )
    await _failure(checks, "ha_get_state", {"entity_id": "input_boolean.native_light"})
    pending = await checks.call("ha_dev_manage_server", {"action": "list_pending"})
    target = next(
        row for row in pending["data"]["pending"] if row["tool_name"] == "ha_get_state"
    )
    await _failure(
        checks,
        "ha_dev_manage_server",
        {"action": "approve", "token": target["token"]},
    )
    allowed = await checks.client.post(
        root + "settings/advanced", json={"dev_tools_security_policy_access": True}
    )
    assert allowed.status_code == 200, allowed.text
    decided = await checks.call(
        "ha_dev_manage_server", {"action": "deny", "token": target["token"]}
    )
    assert decided["data"]["decision"] == "denied", decided
    await _failure(
        checks,
        "ha_dev_manage_server",
        {"action": "approve", "token": target["token"]},
    )
    checks.passed(
        "native_security_document_CAS_and_real_queue_decision_requires_human_opt_in_once"
    )


async def _exercise_reports(checks: Any, root: str) -> None:
    for report_type in ("runtime_bug", "agent_behavior", "feature_request"):
        report = await checks.call(
            "ha_report_issue",
            {
                "report_type": report_type,
                "title": "Native qualification",
                "description": "Local diagnostic preparation; never submitted",
                "client_app": "native-contract",
                "tool_call_count": 5,
            },
        )
        assert (
            report["success"] and report["formatted_report"] and report["instructions"]
        ), report
        serialized = json.dumps(report)
        assert (
            "In-process Core" in serialized
            and "native engine operation records" in serialized
        ), report
        for credential in checks.credentials.values():
            assert credential["access_token"] not in serialized
    checks.passed(
        "native_issue_report_three_types_real_Core_history_unpublished_and_redacted"
    )
