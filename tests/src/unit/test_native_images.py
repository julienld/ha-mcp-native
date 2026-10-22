"""Neutral image results remain bounded and obey both native projection boundaries."""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import native_llm, native_server  # noqa: E402
from ha_mcp import tool_api  # noqa: E402
from ha_mcp._vendor.fastmcp import Client, FastMCP  # noqa: E402
from ha_mcp._vendor.fastmcp.server import create_proxy  # noqa: E402
from ha_mcp.runtime import Engine  # noqa: E402
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolImage, ToolResult  # noqa: E402

from .test_native_visibility import runtime as visibility_runtime  # noqa: E402

_IMAGE = b"bounded image bytes"


def test_neutral_images_enforce_per_image_aggregate_and_count_bounds(monkeypatch):
    monkeypatch.setattr(tool_api, "MAX_TOOL_IMAGE_BYTES", 4)
    monkeypatch.setattr(tool_api, "MAX_RESULT_IMAGE_BYTES", 6)
    monkeypatch.setattr(tool_api, "MAX_RESULT_IMAGES", 2)
    image = ToolImage(b"four", "image/png")
    with pytest.raises(ValueError):
        ToolImage(b"large", "image/png")
    with pytest.raises(ValueError):
        ToolImage(b"four", "text/html")
    with pytest.raises(ValueError):
        ToolResult({}, images=(image, image))
    with pytest.raises(ValueError):
        ToolResult({}, images=(ToolImage(b"x", "image/png"),) * 3)
    assert "four" not in repr(image)


async def test_engine_does_not_retain_image_bytes_in_operation_records():
    engine = Engine(SimpleNamespace(authorize=AsyncMock()))
    result = await engine.call_owned(
        "image",
        AsyncMock(
            return_value=ToolResult(
                {"changed": True}, images=(ToolImage(_IMAGE, "image/png"),)
            )
        ),
        CallContext("owner"),
        writes=True,
    )
    await asyncio.sleep(0)
    assert result.images[0].data == _IMAGE and not engine.tasks
    records = json.dumps(engine.operations)
    assert "bounded image bytes" not in records and "images" not in records


async def test_failed_result_filter_erases_images_and_keeps_confirmed_outcome():
    engine = Engine(
        SimpleNamespace(authorize=AsyncMock()),
        result_filter=AsyncMock(side_effect=ValueError("filter failed")),
    )
    result = await engine.call_owned(
        "image",
        AsyncMock(
            return_value=ToolResult(
                {"changed": True}, images=(ToolImage(_IMAGE, "image/png"),)
            )
        ),
        CallContext("owner"),
        writes=True,
    )
    assert result.is_error and result.status == "confirmed" and result.images == ()
    assert result.data["error"]["code"] == "RESULT_FILTER_FAILED"


async def test_visibility_enforcement_never_passes_unfilterable_images(
    tmp_path, monkeypatch
):
    _manager, engine, _handler = visibility_runtime(
        tmp_path,
        monkeypatch,
        "native_image",
        ToolResult({"changed": True}, images=(ToolImage(_IMAGE, "image/png"),)),
    )
    result = await engine.call("native_image", {}, CallContext("owner"))
    assert result.is_error and result.status == "confirmed" and result.images == ()
    assert result.data["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED"


@pytest.fixture
def image_runtime(monkeypatch):
    backend = SimpleNamespace(authorize=AsyncMock())
    definition = ToolDefinition(
        "native_image",
        "A structured result with an image",
        {"type": "object"},
        AsyncMock(
            return_value=ToolResult(
                {"changed": True}, images=(ToolImage(_IMAGE, "image/png"),)
            )
        ),
    )
    manager = SimpleNamespace(
        backend=backend,
        engine=Engine(backend, [definition]),
        hass=SimpleNamespace(
            async_add_executor_job=lambda fn, *args: asyncio.ensure_future(
                asyncio.to_thread(fn, *args)
            )
        ),
    )
    monkeypatch.setattr(native_server, "current_context", lambda: CallContext("owner"))
    mcp = FastMCP("Image projection")
    mcp.add_tool(native_server._project_tool(manager, definition))
    return manager, mcp


@pytest.mark.parametrize("proxy", [False, True])
async def test_mcp_and_standard_proxy_keep_native_image_blocks_and_outcome(
    image_runtime, proxy
):
    _manager, mcp = image_runtime
    async with Client(create_proxy(mcp) if proxy else mcp) as client:
        result = await client.call_tool("native_image", {})
    assert result.data == {"changed": True}
    images = [block for block in result.content if block.type == "image"]
    assert len(images) == 1 and base64.b64decode(images[0].data) == _IMAGE
    assert images[0].mimeType == "image/png"
    assert result.meta["ha_mcp"]["status"] == "confirmed"
    assert result.meta["ha_mcp"]["operation_id"]


async def test_assist_reports_image_limit_without_base64_or_losing_write_outcome(
    image_runtime, monkeypatch
):
    manager, _mcp = image_runtime
    monkeypatch.setattr(
        native_llm,
        "exposed_catalog",
        AsyncMock(return_value=([SimpleNamespace(name="native_image")], set())),
    )
    monkeypatch.setattr(native_llm, "tool_result", lambda value, **kwargs: value)
    result = await native_llm.call_exposed(
        manager,
        "native_image",
        {},
        SimpleNamespace(
            context=SimpleNamespace(user_id="owner"), platform="test", language="en"
        ),
    )
    assert result["structuredContent"]["changed"] is True
    assert (
        "Assist cannot receive tool images"
        in result["structuredContent"]["warnings"][0]
    )
    assert result["meta"]["ha_mcp"]["status"] == "confirmed"
    assert all(block["type"] != "image" for block in result["content"])
    assert base64.b64encode(_IMAGE).decode() not in json.dumps(result)
