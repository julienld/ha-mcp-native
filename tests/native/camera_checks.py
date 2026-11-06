"""Qualify image bytes, literal captions and Assist limits against real Core."""

import asyncio
import base64
import time
from typing import Any


async def _camera(checks: Any) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while True:
        camera: dict[str, Any] = await checks.control("camera")
        if camera["ready"]:
            return camera
        if time.monotonic() >= deadline:
            raise AssertionError(camera)
        await asyncio.sleep(0.1)


async def exercise_camera(checks: Any) -> None:
    camera = await _camera(checks)
    reply = await checks.rpc(
        "tools/call",
        {
            "name": "ha_get_camera_image",
            "arguments": {
                "entity_id": camera["entity_id"],
                "width": 640,
                "height": 480,
            },
        },
    )
    assert "error" not in reply, reply
    result = reply["result"]
    assert not result.get("isError"), result
    text, image = result["content"]
    assert text["type"] == "text" and text["text"].startswith(
        "Camera snapshot (PNG, 1x1). Retrieved:"
    ), text
    assert image["type"] == "image" and image["mimeType"] == "image/png", image
    pixels = base64.b64decode(image["data"], validate=True)
    assert pixels.startswith(b"\x89PNG\r\n\x1a\n") and len(pixels) > 32
    assert not result.get("structuredContent"), result
    reread = await checks.control("camera")
    assert reread["calls"][-1] == {"width": 640, "height": 480}, reread
    assert result["_meta"]["ha_mcp"]["status"] == "confirmed", result
    checks.passed("native_camera_ImageContent_and_plain_caption_use_real_Core_Camera")


async def exercise_camera_assist(checks: Any) -> None:
    camera = await _camera(checks)
    result = await checks.control(
        "llm",
        {
            "tool_name": "ha_get_camera_image",
            "arguments": {"entity_id": camera["entity_id"]},
        },
    )
    assert result["error"] and "IMAGE_CONTENT_UNSUPPORTED" in str(result), result
    assert (await checks.control("camera"))["calls"] == camera["calls"]
    checks.passed("Assist_refuses_image_only_tools_before_real_camera_capture")
