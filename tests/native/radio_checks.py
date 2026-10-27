"""Qualify honest hardware-absent radio behavior through the native HTTP server.

The Core Container fixture has no ZHA, Z-Wave JS, Matter or OTBR config entry.
This bench deliberately makes no claim about physical commissioning, firmware,
channels or mesh routing; loaded-owner unit tests and pinned Core source cover
those contracts until an explicitly authorized hardware fixture is available.
No fixture hooks or installed optional radio dependencies are required.
"""

import json
from typing import Any


async def exercise_radios(checks: Any) -> None:
    for radio in ("zwave", "zigbee", "matter", "thread"):
        result = await checks.call(
            "ha_manage_radio", {"radio": radio, "action": "network_status"}
        )
        assert (
            result["success"] and result["available"] is False and result["warnings"]
        ), result
    checks.passed("native_radio_hardware_absence_is_explicit_for_all_four_families")
    cases = (
        ("zwave", "add", {"dsk": "native-fixture"}),
        ("zigbee", "permit_join", {"duration": 0}),
        ("matter", "commission", {"code": "native-fixture"}),
        ("thread", "set_channel", {"channel": 20}),
    )
    for radio, action, params in cases:
        body = await checks.rpc(
            "tools/call",
            {
                "name": "ha_manage_radio",
                "arguments": {
                    "radio": radio,
                    "action": action,
                    "params": params,
                    "confirm": True,
                },
            },
        )
        assert "error" not in body and body["result"].get("isError"), body
        result = body["result"]
        payload = result.get("structuredContent") or json.loads(
            next(
                block["text"] for block in result["content"] if block["type"] == "text"
            )
        )
        assert payload["error"]["code"] == "RADIO_UNAVAILABLE", payload
    checks.passed(
        "native_radio_missing_hardware_writes_are_errors_without_transport_fallback"
    )
