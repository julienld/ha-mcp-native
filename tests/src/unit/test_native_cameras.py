"""Core cameras retain provider ownership, authorization and bounded image output."""

import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native
from .test_tools_camera import _png


@pytest.fixture
def cameras(native, monkeypatch):
    from custom_components.ha_mcp_tools.native_camera_tools import camera_definition
    from custom_components.ha_mcp_tools.native_operations import cameras as module

    world = SimpleNamespace(
        owner=SimpleNamespace(is_on=True),
        data=_png(640, 480),
        declared="image/jpeg",
        calls=[],
        threads=[],
        block=False,
        started=asyncio.Event(),
        release=asyncio.Event(),
    )

    async def image(hass, entity_id, timeout, width, height):
        world.calls.append((entity_id, width, height))
        world.started.set()
        if world.block:
            await world.release.wait()
        return SimpleNamespace(content=world.data, content_type=world.declared)

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.camera",
        SimpleNamespace(
            CAMERA_IMAGE_TIMEOUT=10,
            async_get_image=image,
            get_camera_from_entity_id=lambda hass, entity_id: world.owner,
        ),
    )

    async def executor(function, *args):
        def run():
            world.threads.append(threading.get_ident())
            return function(*args)

        return await asyncio.to_thread(run)

    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="America/Toronto"),
        async_add_executor_job=executor,
    )
    backend = SimpleNamespace(authorize=AsyncMock())
    manager = SimpleNamespace(hass=hass, backend=backend)
    engine = Engine(backend, [camera_definition(manager)])
    return manager, engine, world, module


async def test_served_bytes_determine_label_size_and_MCP_media_type(cameras):
    _, engine, world, _ = cameras
    result = await engine.call(
        "ha_get_camera_image",
        {"entity_id": "camera.front", "width": 640, "height": 480},
        CallContext("actor"),
    )
    assert not result.is_error and result.status == "confirmed"
    assert result.images[0].data == world.data
    assert result.images[0].mime_type == "image/png"
    assert result.data.startswith("Camera snapshot (PNG, 640x480). Retrieved:")
    assert "-04:00" in result.data or "-05:00" in result.data
    assert world.calls == [("camera.front", 640, 480)]
    assert world.threads and threading.get_ident() not in world.threads


def test_native_camera_preserves_inputs_and_has_no_synthetic_output_schema(cameras):
    from .test_native_catalog_contract import _ANNOTATIONS, _changed_inputs

    _, engine, _, _ = cameras
    baseline = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "docs/native-migration/catalog-baseline.json"
        ).read_text(encoding="utf-8")
    )["tools"]
    old = next(row for row in baseline if row["name"] == "ha_get_camera_image")
    actual = engine.tools["ha_get_camera_image"]
    assert not _changed_inputs(old["inputSchema"], actual.input_schema)
    assert all(
        actual.annotations.get(key) == old["annotations"].get(key)
        for key in _ANNOTATIONS
    )
    assert actual.description == old["description"]
    assert actual.output_schema is None


async def test_swapped_camera_owner_cannot_release_the_old_image(cameras):
    _, engine, world, _ = cameras
    world.block = True
    task = asyncio.create_task(
        engine.call(
            "ha_get_camera_image", {"entity_id": "camera.front"}, CallContext("actor")
        )
    )
    await world.started.wait()
    world.owner = SimpleNamespace(is_on=True)
    world.release.set()
    result = await task
    assert result.is_error and result.images == ()
    assert result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert len(world.calls) == 1


async def test_revoked_actor_during_provider_read_cannot_receive_pixels(cameras):
    manager, engine, world, _ = cameras
    world.block = True
    task = asyncio.create_task(
        engine.call(
            "ha_get_camera_image", {"entity_id": "camera.front"}, CallContext("actor")
        )
    )
    await world.started.wait()
    manager.backend.authorize.side_effect = PermissionError("actor revoked")
    world.release.set()
    result = await task
    assert result.is_error and result.images == ()
    assert result.data["error"]["code"] == "AUTH_FORBIDDEN"


async def test_off_camera_does_not_invoke_the_provider(cameras):
    _, engine, world, _ = cameras
    world.owner.is_on = False
    result = await engine.call(
        "ha_get_camera_image", {"entity_id": "camera.front"}, CallContext("actor")
    )
    assert result.is_error and result.images == () and world.calls == []


@pytest.mark.parametrize("payload", [b"", b"over the supported bound"])
async def test_empty_and_oversized_images_are_not_projected(
    cameras, monkeypatch, payload
):
    _, engine, world, module = cameras
    monkeypatch.setattr(module, "MAX_TOOL_IMAGE_BYTES", 4)
    world.data = payload
    result = await engine.call(
        "ha_get_camera_image", {"entity_id": "camera.front"}, CallContext("actor")
    )
    assert result.is_error and result.images == ()
    assert result.data["error"]["code"] == "IMAGE_SIZE_UNSUPPORTED"


async def test_cancelled_read_drains_the_owned_provider_before_return(cameras):
    manager, _, world, module = cameras
    service = module.NativeCameras(
        manager.hass, CallContext("actor"), authorize=manager.backend.authorize
    )
    world.block = True
    task = asyncio.create_task(service.snapshot("camera.front"))
    await world.started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    world.release.set()
    with pytest.raises(asyncio.CancelledError):
        _ = await task
    assert len(world.calls) == 1
