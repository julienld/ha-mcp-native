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

    changed = await _raw_apis(checks)
    await _raw_boundaries(checks, base)
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
        "native_code_known_raw_APIs_use_Core_owners_and_unknown_commands_and_recursion_are_refused"
    )
    raw_service = "await api_post('/services/input_boolean/toggle', {'entity_id': 'input_boolean.native_light'})"
    await _approval_gate(checks, base, raw_service)
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


async def _raw_apis(checks: Any) -> dict[str, Any]:
    before = await checks.control("code_probe")
    raw = await checks.call(
        "ha_manage_custom_tool",
        {
            "code": "[await api_get('/config'), await api_get('/states/input_boolean.native_light'), await api_get('/states'), await api_get('/services'), await api_post('/template', {'template': '{{ 40 + 2 }}'}), await ws_send({'type': 'config/area_registry/list'}), await ws_send({'type': 'get_states'}), await api_get('/native_unported'), await api_post('/native_unported', {}), await ws_send({'type': 'native_unported/command'})]",
            "justification": "Qualify bounded native API compatibility and explicit limits",
        },
    )
    config, state, states, services, template, areas, ws_states, *unknown = raw["data"][
        "result"
    ]
    assert "location_name" in config and "time_zone" in config, raw
    assert (
        state["entity_id"] == "input_boolean.native_light" and state["context"]["id"]
    ), raw
    assert any(row["entity_id"] == state["entity_id"] for row in states), raw
    assert any(row["entity_id"] == state["entity_id"] for row in ws_states), raw
    assert any(row["domain"] == "input_boolean" for row in services), raw
    assert template == 42 and isinstance(areas, list), raw
    assert len(unknown) == 3 and all(
        item["error"]["code"] == "UNSUPPORTED_OPERATION" for item in unknown
    ), raw
    assert (await checks.control("code_probe"))["state"] == before["state"]
    posted = await checks.call(
        "ha_manage_custom_tool",
        {
            "code": "await api_post('/services/input_boolean/toggle', {'entity_id': 'input_boolean.native_light'})",
            "justification": "Qualify native raw service attribution",
        },
    )
    after = await checks.control("code_probe")
    assert (
        after["state"] != before["state"]
        and after["state_user_id"] == after["request_user_id"]
    ), after
    changed_state = next(
        row
        for row in posted["data"]["result"]
        if row["entity_id"] == "input_boolean.native_light"
    )
    assert (
        changed_state["context"]["id"]
        and changed_state["context"]["user_id"] == after["request_user_id"]
    ), posted
    return after


async def _raw_boundaries(checks: Any, base: str) -> None:
    path = checks.path
    try:
        checks.path += "/readonly/catalog/full"
        denied = await checks.rpc(
            "tools/call",
            {
                "name": "ha_manage_custom_tool",
                "arguments": {
                    "code": "await api_get('/states')",
                    "justification": "Read-only raw-code refusal",
                },
            },
        )
        assert denied["result"].get("isError") and "READ_ONLY" in json.dumps(denied), (
            denied
        )
    finally:
        checks.path = path
    tools_url = base + "/api/ha_mcp_tools/ui/app/api/settings/tools"
    response = await checks.client.get(tools_url)
    response.raise_for_status()
    original = response.json()
    changed = await checks.client.post(
        tools_url, json={"states": {"ha_list_services": "disabled"}}
    )
    changed.raise_for_status()
    try:
        result = await checks.call(
            "ha_manage_custom_tool",
            {
                "code": "await api_get('/services')",
                "justification": "Raw helpers preserve tool admission",
            },
        )
        assert result["data"]["result"]["error"]["code"] == "TOOL_DISABLED", result
    finally:
        restored = await checks.client.post(
            tools_url,
            json={
                "states": original["states"],
                "llm_api": original["llm_api_overrides"],
            },
        )
        restored.raise_for_status()
    checks.passed(
        "native_code_raw_helpers_preserve_readonly_and_disabled_tool_admission"
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
