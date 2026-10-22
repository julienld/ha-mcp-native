"""Real Core LLM API calls share the native action and authorization paths."""

import json
from typing import Any

from camera_checks import exercise_camera_assist
from screenshot_checks import exercise_screenshot_assist


async def exercise_llm(checks: Any) -> None:
    await exercise_templates(checks)
    await checks.control("configure", {})
    await checks.ready()
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
