"""Qualify the real conditional Monty owner, persistence and nested policy gates."""

import json
from typing import Any

from system_checks import exercise_system_self_reload


async def exercise_code_mode(checks: Any, base: str) -> None:
    panel = base + "/api/ha_mcp_tools/ui/app/api/settings"
    before = await checks.control("code_probe")
    assert before["available"] is False, before
    response = await checks.client.post(
        base + "/api/ha_mcp_tools/ui/session", headers=checks.headers()
    )
    response.raise_for_status()
    configured = await checks.client.post(
        panel + "/advanced", json={"code_mode_saved_tools_path": before["saved_path"]}
    )
    assert configured.status_code == 200, configured.text
    enabled = await checks.client.post(
        panel + "/features",
        json={"flags": {"enable_beta_features": True, "enable_code_mode": True}},
    )
    assert enabled.status_code == 200 and enabled.json()["restart_required"], (
        enabled.text
    )
    await exercise_system_self_reload(checks)
    assert (await checks.control("code_probe"))["available"] is True
    script = "await call_tool('ha_call_service', {'domain': 'input_boolean', 'service': 'toggle', 'entity_id': 'input_boolean.native_light'})"
    created = await checks.call(
        "ha_manage_custom_tool",
        {
            "code": script,
            "justification": "Qualify native tool composition",
            "save_as": "native_contract",
        },
    )
    assert (
        created["data"]["saved_as"] == "native_contract"
        and created["data"]["result"]["success"]
    ), created
    alpha = await checks.control("code_probe")
    assert alpha["state_user_id"] == alpha["request_user_id"], alpha
    listed = await checks.call("ha_manage_custom_tool", {"list_saved": True})
    assert listed["data"]["saved_tools"]["native_contract"]["code"] == script, listed
    await exercise_system_self_reload(checks)
    response = await checks.rpc(
        "tools/call",
        {
            "name": "ha_manage_custom_tool",
            "arguments": {"run_saved": "native_contract"},
        },
        user="beta",
    )
    assert "error" not in response and not response["result"].get("isError"), response
    beta = await checks.client.get(
        base + "/api/native_fixture/code_probe", headers=checks.headers("beta")
    )
    beta.raise_for_status()
    changed = beta.json()
    assert (
        changed["state"] != alpha["state"]
        and changed["state_user_id"] == changed["request_user_id"]
    ), changed
    checks.passed(
        "native_code_opt_in_Monty_saved_script_survives_reload_and_runs_as_current_actor"
    )

    raw = await checks.call(
        "ha_manage_custom_tool",
        {
            "code": "[await api_get('/states'), await api_post('/services/input_boolean/toggle', {}), await ws_send({'type': 'get_states'})]",
            "justification": "Qualify explicit native compatibility limits",
        },
    )
    assert len(raw["data"]["result"]) == 3 and all(
        item["error"]["code"] == "UNSUPPORTED_OPERATION"
        for item in raw["data"]["result"]
    ), raw
    recursive = await checks.call(
        "ha_manage_custom_tool",
        {
            "code": "await call_tool('ha_manage_custom_tool', {'list_saved': True})",
            "justification": "Qualify recursive proxy refusal",
        },
    )
    assert (
        recursive["data"]["result"]["error"]["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"
    ), recursive
    assert (await checks.control("code_probe"))["state"] == changed["state"]
    checks.passed(
        "native_code_raw_ABI_explicitly_unavailable_and_recursive_tool_reentry_refused"
    )
    await _approval_gate(checks, base, script)
    deleted = await checks.call(
        "ha_manage_custom_tool",
        {
            "code": "await delete_saved_tool('native_contract')",
            "justification": "Clean up the qualification script",
        },
    )
    assert deleted["data"]["result"]["deleted"], deleted
    await exercise_system_self_reload(checks)
    listed = await checks.call("ha_manage_custom_tool", {"list_saved": True})
    assert "native_contract" not in listed["data"]["saved_tools"], listed
    checks.passed(
        "native_code_saved_script_deletion_persists_through_real_entry_reload"
    )


async def _approval_gate(checks: Any, base: str, script: str) -> None:
    policy_url = base + "/api/ha_mcp_tools/ui/app/api/policy/config"
    feature_url = base + "/api/ha_mcp_tools/ui/app/api/settings/features"
    features = await checks.client.get(feature_url)
    features.raise_for_status()
    was_enabled = features.json()["flags"]["enable_tool_security_policies"]["value"]
    enabled = await checks.client.post(
        feature_url, json={"flags": {"enable_tool_security_policies": True}}
    )
    assert enabled.status_code == 200, enabled.text
    response = await checks.client.get(policy_url)
    response.raise_for_status()
    original = response.json()
    temporary = {
        **original,
        "rule_effect": "require_approval",
        "wait_seconds": 5,
        "rules": [{"tool_name": "ha_call_service"}],
    }
    before = await checks.control("code_probe")
    saved = await checks.client.put(policy_url, json=temporary)
    assert saved.status_code == 200, saved.text
    try:
        result = await checks.call(
            "ha_manage_custom_tool",
            {
                "code": script,
                "justification": "Qualify policy enforcement inside saved-code composition",
            },
        )
        assert result["data"]["operations"][0]["status"] == "pending", result
        assert result["data"]["result"]["success"] is False, result
        assert (await checks.control("code_probe"))["state"] == before["state"]
        assert "continuation" not in json.dumps(result["data"]["result"]).lower(), (
            result
        )
    finally:
        current = await checks.client.get(policy_url)
        current.raise_for_status()
        restored = await checks.client.put(
            policy_url, json={**original, "version": current.json()["version"]}
        )
        assert restored.status_code == 200, restored.text
        restored_flags = await checks.client.post(
            feature_url, json={"flags": {"enable_tool_security_policies": was_enabled}}
        )
        assert restored_flags.status_code == 200, restored_flags.text
    checks.passed(
        "native_code_nested_approval_does_not_dispatch_or_capture_outer_script_for_replay"
    )
