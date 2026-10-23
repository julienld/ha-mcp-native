"""Real Core LLM API calls share the native action and authorization paths."""

import json
from typing import Any

from camera_checks import exercise_camera_assist
from screenshot_checks import exercise_screenshot_assist


async def assert_bridges(checks: Any, *, export: bool, importing: bool) -> None:
    probe = await checks.ready()
    identifiers = {
        f"ha_mcp_tools-{probe['entry_id']}",
        f"ha_mcp_tools-{probe['entry_id']}-toolsearch",
    }
    registered = identifiers.intersection(probe["llm_apis"])
    assert registered == (identifiers if export else set()), probe
    names = {
        tool["name"] for tool in (await checks.rpc("tools/list"))["result"]["tools"]
    }
    imported = {"ha_native_llm_tools", "ha_native_llm_call"}.intersection(names)
    assert imported == (
        {"ha_native_llm_tools", "ha_native_llm_call"} if importing else set()
    ), imported
    assert "native_fixture" in probe["llm_apis"], probe
    assert "ha_get_state" in names, names


async def exercise_llm_defaults(checks: Any) -> None:
    await assert_bridges(checks, export=False, importing=False)
    checks.passed("new_entry_without_options_leaves_both_LLM_bridges_disabled")


async def exercise_independent_bridges(checks: Any) -> None:
    for exporting, importing in ((True, False), (False, True), (False, False)):
        await checks.control("configure", {"export": exporting, "import": importing})
        await assert_bridges(checks, export=exporting, importing=importing)
    checks.passed("LLM_bridges_toggle_independently_with_selected_APIs_retained")


async def exercise_llm(checks: Any) -> None:
    await exercise_templates(checks)
    await exercise_independent_bridges(checks)
    await checks.control("configure", {})
    await assert_bridges(checks, export=True, importing=True)
    await exercise_camera_assist(checks)
    await exercise_screenshot_assist(checks)
    result = await checks.control(
        "llm",
        {
            "tool_name": "ha_get_state",
            "arguments": {
                "entity_id": "sensor.native_fixture",
            },
        },
    )
    assert not result["error"] and "ready" in json.dumps(result), result
    checks.passed("shared_tool_exported_to_Assist")
    result = await checks.control(
        "llm",
        {
            "tool_name": "ha_call_service",
            "arguments": {
                "domain": "input_boolean",
                "service": "turn_off",
                "entity_id": "input_boolean.native_light",
            },
        },
    )
    assert not result["error"], result
    state = await checks.call(
        "ha_get_state", {"entity_id": "input_boolean.native_light"}
    )
    assert state["data"]["state"] == "off", state
    checks.passed("shared_tool_action_executed_from_Assist_without_loopback")
    imported = await checks.call("ha_native_llm_tools", {"api_id": "native_fixture"})
    assert "native_fixture/NativeEcho" in json.dumps(imported), imported
    reply = await checks.call(
        "ha_native_llm_call",
        {
            "name": "native_fixture/NativeEcho",
            "arguments": {"text": "native"},
        },
    )
    assert "NATIVE" in json.dumps(reply), reply
    checks.passed("native_LLM_API_import_with_original_validator")
    result = await checks.call(
        "ha_native_llm_call", {"name": "native_fixture/NativeTurnOn", "arguments": {}}
    )
    assert "turned_on" in json.dumps(result), result
    state = await checks.call(
        "ha_get_state", {"entity_id": "input_boolean.native_light"}
    )
    assert state["data"]["state"] == "on", state
    checks.passed("Core_LLM_API_action_executed_from_MCP_without_loopback")


async def exercise_templates(checks: Any) -> None:
    rendered = await checks.call(
        "ha_eval_template",
        {
            "template": "{{ states('sensor.' + suffix) }}",
            "variables": {"suffix": "native_fixture"},
        },
    )
    assert rendered["result"] == "ready", rendered
    condition = await checks.call(
        "ha_eval_template",
        {
            "condition": {
                "condition": "state",
                "entity_id": "sensor.native_fixture",
                "state": "ready",
            }
        },
    )
    assert condition["result"] is True, condition
    checks.passed("canonical_Core_template_render_and_condition_without_WebSocket")
