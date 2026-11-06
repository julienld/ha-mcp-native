"""Qualify real HACS absence in the native Core Container, without credentials.

This bench does not install or impersonate HACS. Core's bundled Marketplace is
explicitly ignored through its config-flow owner before startup. No active store
is qualified here. An unconfigured optional integration cannot trigger GH requests, file
changes or fallback Home Assistant transports through either public tool.
Installed HACS behavior is separately supported by boundary unit tests and
review of official HACS 2.0.5 c0dfd8b44297c3673c21973e2539375a53687a9c.
Live registration/download/refresh remains unqualified without actual HACS.
"""

import json
from typing import Any


async def exercise_hacs_absence(checks: Any) -> None:
    probe = await checks.control("store_probe")
    assert not probe["hacs_loaded"] and probe["marketplace_loaded"] == 0, probe
    assert all(row["source"] == "ignore" for row in probe["marketplace_entries"]), probe
    expected = (
        "CONFIG_CONFLICT"
        if probe["marketplace_component"]
        else "COMPONENT_NOT_INSTALLED"
    )
    scenarios = [
        ("ha_get_hacs_info", {"action": "search"}),
        ("ha_get_hacs_info", {"action": "info", "repository_id": "fixture/absent"}),
        (
            "ha_manage_hacs",
            {
                "action": "add_repository",
                "repository": "fixture/absent",
                "category": "integration",
            },
        ),
    ]
    scenarios.extend(
        ("ha_manage_hacs", {"action": action, "repository_id": "fixture/absent"})
        for action in ("download", "remove", "update_information")
    )
    for name, arguments in scenarios:
        body = await checks.rpc("tools/call", {"name": name, "arguments": arguments})
        assert "error" not in body and body["result"].get("isError"), body
        result = body["result"]
        payload = result.get("structuredContent") or json.loads(
            next(
                block["text"] for block in result["content"] if block["type"] == "text"
            )
        )
        assert payload["error"]["code"] == expected, payload
        if probe["marketplace_component"]:
            assert (
                "Expected one loaded Marketplace owner" in payload["error"]["message"]
            ), payload
    checks.passed(
        "native_HACS_absence_is_explicit_for_both_tools_and_all_actions_without_GitHub_credentials"
    )
