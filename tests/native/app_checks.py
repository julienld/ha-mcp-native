"""Container proves explicit Supervisor absence, not HAOS app operations."""

import json
from typing import Any


async def exercise_apps(checks: Any) -> None:
    for name, arguments in (
        ("ha_get_app", {}),
        ("ha_manage_app", {"slug": "core_native_fixture", "action": "start"}),
    ):
        response = await checks.rpc(
            "tools/call", {"name": name, "arguments": arguments}
        )
        envelope = response["result"]
        assert envelope.get("isError"), response
        result = envelope.get("structuredContent") or json.loads(
            next(
                block["text"]
                for block in envelope["content"]
                if block["type"] == "text"
            )
        )
        assert result.get("success") is False, result
        assert result["error"]["code"] == "UNSUPPORTED_OPERATION", result
    checks.passed("apps_supervisor_absence_is_explicit")
