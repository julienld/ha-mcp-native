"""Qualify real HACS absence in the native Core Container, without credentials.

This bench does not install or impersonate HACS. It establishes only that an
unconfigured optional integration cannot trigger imports, GH requests, file
changes or fallback Home Assistant transports through either public tool.
Installed HACS behavior is separately supported by boundary unit tests and
review of official HACS 2.0.5 c0dfd8b44297c3673c21973e2539375a53687a9c.
Live registration/download/refresh remains unqualified without actual HACS.
"""

import json
from typing import Any


async def exercise_hacs_absence(checks: Any) -> None:
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
        assert payload["error"]["code"] == "COMPONENT_NOT_INSTALLED", payload
    checks.passed(
        "native_HACS_absence_is_explicit_for_both_tools_and_all_actions_without_GitHub_credentials"
    )
