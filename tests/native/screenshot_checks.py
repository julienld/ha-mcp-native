"""Qualify native discovery and refusals without claiming a real renderer run."""

import json
from typing import Any

from dashboard_checks import _failure


async def exercise_screenshots(checks: Any, base: str) -> None:
    session = await checks.client.post(
        base + "/api/ha_mcp_tools/ui/session", headers=checks.headers()
    )
    session.raise_for_status()
    response = await checks.client.post(
        base + "/api/ha_mcp_tools/ui/app/api/settings/features",
        json={
            "flags": {"enable_beta_features": True, "enable_dashboard_screenshot": True}
        },
    )
    assert response.status_code == 200, response.text
    response = await checks.client.post(
        base + "/api/ha_mcp_tools/ui/app/api/settings/tools",
        json={"llm_api": {"ha_get_dashboard_screenshot": True}},
    )
    assert response.status_code == 200, response.text
    catalog = (await checks.rpc("tools/list"))["result"]["tools"]
    definition = next(
        tool for tool in catalog if tool["name"] == "ha_get_dashboard_screenshot"
    )
    assert definition["annotations"]["readOnlyHint"] is False, definition
    refused = await _failure(
        checks, "ha_get_dashboard_screenshot", {"url_path": "default"}
    )
    payload = refused.get("structuredContent") or json.loads(
        next(block["text"] for block in refused["content"] if block["type"] == "text")
    )
    assert payload["error"]["code"] == "SERVICE_CALL_FAILED", payload
    assert refused["_meta"]["ha_mcp"]["status"] == "not_started", refused
    assert not any(block["type"] == "image" for block in refused["content"])
    checks.passed(
        "native_screenshot_write_discovery_and_honest_absent_renderer_refusal"
    )
    invalid = await _failure(
        checks, "ha_get_dashboard_screenshot", {"dashboard_path": "config/integrations"}
    )
    assert "RESOURCE_NOT_FOUND" in json.dumps(invalid), invalid
    checks.passed("native_screenshot_raw_routes_require_a_real_Core_Lovelace_owner")


async def exercise_screenshot_assist(checks: Any) -> None:
    result = await checks.control(
        "llm",
        {
            "tool_name": "ha_get_dashboard_screenshot",
            "arguments": {"url_path": "default"},
        },
    )
    assert result["error"] and "IMAGE_CONTENT_UNSUPPORTED" in str(result), result
    checks.passed("Assist_refuses_image_only_screenshots_before_renderer_discovery")
