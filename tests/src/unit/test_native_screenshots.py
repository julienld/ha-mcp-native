"""Native screenshots reuse the HTTP engine without constructing an HA client."""

import asyncio
import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ha_mcp.tool_api import CallContext, ToolFailure, ToolResult

from .test_native_engine_backend import native as native


@pytest.fixture
def renderer(native, monkeypatch):
    from custom_components.ha_mcp_tools.native_screenshots import NativeScreenshots
    from custom_components.ha_mcp_tools.native_server_policy import NativePolicy
    from ha_mcp.dashboard_screenshot import capture

    monkeypatch.delenv("HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL", raising=False)
    active = [True]

    async def authorize(actor):
        if not active[0]:
            raise PermissionError("revoked")

    hass = SimpleNamespace(
        config=SimpleNamespace(components=set()),
        async_add_executor_job=lambda fn, *args: asyncio.ensure_future(
            asyncio.to_thread(fn, *args)
        ),
    )
    manager = SimpleNamespace(
        hass=hass,
        backend=SimpleNamespace(authorize=authorize),
        policy=NativePolicy(
            flags={
                "dashboard_screenshot_engine_url": "http://renderer.test:10000",
                "enable_beta_features": True,
                "enable_dashboard_screenshot": True,
            }
        ),
    )
    client = httpx.AsyncClient
    requests = []
    status = [200]
    png = b"\x89PNG\r\n\x1a\nfixture"

    async def handle(request):
        requests.append(request)
        if len(status) > 1:
            return httpx.Response(
                status[0], headers={"content-type": "image/png"}, stream=status[1]
            )
        return httpx.Response(
            status[0], headers={"content-type": "image/png"}, content=png
        )

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        capture.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=transport, **kwargs),
    )
    forbidden = AsyncMock(
        side_effect=AssertionError("Legacy engine discovery must not run")
    )
    monkeypatch.setattr(capture, "resolve_engine", forbidden)
    return (
        NativeScreenshots(manager, CallContext("owner")),
        requests,
        status,
        active,
        png,
    )


async def test_native_capture_sends_only_render_parameters_to_configured_engine(
    renderer,
):
    renderer, requests, _status, _active, png = renderer
    result = await renderer.capture(
        {"success": True},
        "wall-panel",
        {"views": [{"path": "home"}]},
        "home",
        after_write=False,
    )
    assert isinstance(result, ToolResult) and result.images[0].data == png
    assert result.data["screenshot_render_path"] == "wall-panel/home"
    assert result.data["screenshots"][0]["image"]["size_bytes"] == len(png)
    assert len(requests) == 1 and requests[0].url.host == "renderer.test"
    assert (
        requests[0].url.path == "/wall-panel/home"
        and "authorization" not in requests[0].headers
    )
    assert any(
        "credential is unavailable" in warning for warning in result.data["warnings"]
    )


async def test_screenshot_failure_after_write_preserves_the_existing_outcome(renderer):
    renderer, _requests, status, *_ = renderer
    status[0] = 500
    data = {"success": True, "write_committed": True, "config_hash": "saved"}
    result = await renderer.capture(
        data, "wall-panel", {"views": []}, None, after_write=True
    )
    assert result["write_committed"] is True and result["config_hash"] == "saved"
    assert result["screenshot_error"] and result["warnings"]
    with pytest.raises(ToolFailure):
        await renderer.capture({}, "wall-panel", {"views": []}, None, after_write=False)


async def test_revocation_during_image_projection_returns_no_image_or_config(
    renderer, monkeypatch
):
    renderer, requests, _status, active, _png = renderer
    original = renderer.manager.hass.async_add_executor_job

    def revoked(function, *args):
        active[0] = False
        return original(function, *args)

    renderer.manager.hass.async_add_executor_job = revoked
    with pytest.raises(ToolFailure) as error:
        await renderer.capture(
            {"private": "already-written-config"},
            "wall-panel",
            {"views": []},
            None,
            after_write=True,
        )
    assert error.value.code == "AUTH_FORBIDDEN" and len(requests) == 1
    assert "private" not in error.value.data


async def test_native_theme_observer_uses_identified_core_user_store_only(
    native, monkeypatch
):
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.screenshot_theme"
    )

    class UserStore:
        def __init__(self):
            self.data = {"theme": {"dark": False}}
            self.async_set_item = AsyncMock()

    store = UserStore()
    load = AsyncMock(return_value=store)
    future = asyncio.get_running_loop().create_future()
    future.set_result(store)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.frontend.storage",
        SimpleNamespace(
            async_user_store=load,
            UserStore=UserStore,
            DATA_STORAGE="frontend_user_data",
        ),
    )

    def validate(token):
        return (
            SimpleNamespace(user=SimpleNamespace(id="renderer-user", is_active=True))
            if token == "engine-placeholder"
            else None
        )

    hass = SimpleNamespace(
        auth=SimpleNamespace(async_validate_access_token=validate),
        data={"frontend_user_data": {"renderer-user": future}},
    )
    observer = module.NativeThemeObserver(
        hass,
        CallContext("admin"),
        AsyncMock(),
        token="engine-placeholder",
        settle_seconds=0,
    )
    await observer.take_snapshot()
    store.data["theme"] = {"dark": True}
    await observer.detect_change()
    assert observer.taken and "changed" in observer.warnings[0]
    load.assert_not_awaited()
    store.async_set_item.assert_not_awaited()
    assert "engine-placeholder" not in repr(observer) + json.dumps(observer.warnings)


@pytest.mark.parametrize("pending", [False, True])
async def test_screenshot_theme_observation_never_initializes_or_cancels_a_user_store(
    native, monkeypatch, pending
):
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.screenshot_theme"
    )

    class UserStore:
        def __init__(self):
            self.data = {"theme": {"dark": False}}

    load = AsyncMock(return_value=UserStore())
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.frontend.storage",
        SimpleNamespace(
            async_user_store=load,
            UserStore=UserStore,
            DATA_STORAGE="frontend_user_data",
        ),
    )
    future = asyncio.get_running_loop().create_future()
    hass = SimpleNamespace(
        auth=SimpleNamespace(
            async_validate_access_token=lambda _: SimpleNamespace(
                user=SimpleNamespace(id="renderer-user", is_active=True)
            )
        ),
        data={"frontend_user_data": {"renderer-user": future}} if pending else {},
    )
    observer = module.NativeThemeObserver(
        hass,
        CallContext("admin"),
        AsyncMock(),
        token="engine-placeholder",
        settle_seconds=0,
    )
    await observer.take_snapshot()
    assert not observer.taken and observer.warnings
    load.assert_not_awaited()
    assert not future.cancelled()


async def test_explicit_renderer_never_inherits_a_different_apps_identity(
    renderer, monkeypatch
):
    renderer, *_ = renderer
    renderer.manager.hass.config.components = {"hassio"}
    monkeypatch.setattr(
        type(renderer),
        "_discover",
        AsyncMock(return_value=("http://another-renderer:10000", "secret-placeholder")),
    )
    target, token = await renderer._target()
    assert target == "http://renderer.test:10000" and token is None


async def test_supervisor_discovery_uses_the_existing_core_client(
    renderer, monkeypatch
):
    renderer, *_ = renderer
    renderer.manager.hass.config.components = {"hassio"}
    renderer.manager.policy.flags["dashboard_screenshot_engine_url"] = ""
    info = {
        "name": "Puppet",
        "schema": [
            {"name": name}
            for name in ("access_token", "home_assistant_url", "keep_browser_open")
        ],
        "state": "started",
        "hostname": "core-puppet",
        "options": {"access_token": "engine-placeholder"},
    }
    addons = SimpleNamespace(
        list=AsyncMock(return_value=[SimpleNamespace(slug="repository_puppet")]),
        addon_info=AsyncMock(return_value=SimpleNamespace(to_dict=lambda: info)),
    )
    client = SimpleNamespace(addons=addons)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.hassio",
        SimpleNamespace(get_supervisor_client=lambda hass: client),
    )
    target, token = await renderer._target()
    assert target == "http://core-puppet:10000" and token == "engine-placeholder"
    addons.addon_info.assert_awaited_once_with("repository_puppet")


async def test_repeated_cancellation_drains_screenshot_response_close(renderer):
    renderer, _requests, status, _active, png = renderer
    closing, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield png

        async def aclose(self):
            closing.set()
            await release.wait()
            closed.set()

    status.append(Stream())
    task = asyncio.create_task(
        renderer.capture({}, "wall-panel", {"views": []}, None, after_write=False)
    )
    try:
        await asyncio.wait_for(closing.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert closed.is_set() and task.cancelled()
