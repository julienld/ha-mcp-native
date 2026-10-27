"""Native app policies apply to every mode before any opaque API request."""

import asyncio
import copy
import importlib
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from yarl import URL

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_app_actions import actions as actions
from .test_native_apps import apps as apps
from .test_native_engine_backend import native as native


@pytest.fixture
def bound(actions: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    actions.manager.hass.async_add_executor_job = asyncio.to_thread
    info = actions.replies["/addons/core_fixture/info"]
    info.update(ingress=True, ingress_entry="/api/hassio_ingress/private-app-token")

    class Ingress:
        name = "api:hassio:ingress"

        def __init__(self) -> None:
            self._websession = SimpleNamespace(closed=False)

        def _create_url(self, token: str, path: str) -> URL:
            assert token == "private-app-token"
            return URL("http://supervisor").with_path(f"/ingress/{token}/{path}")

    view = Ingress()

    def handler() -> Any:
        return view

    # Actual Core's fixed route closure calls this binding 'view'.
    actions.manager.hass.http = SimpleNamespace(
        app=SimpleNamespace(
            router=SimpleNamespace(
                routes=lambda: [
                    SimpleNamespace(
                        method="GET",
                        resource=SimpleNamespace(
                            canonical="/api/hassio_ingress/{token}/{path}"
                        ),
                        handler=handler,
                    )
                ]
            )
        )
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.hassio.ingress",
        SimpleNamespace(HassIOIngress=Ingress),
    )
    monkeypatch.setitem(
        sys.modules,
        "aiohasupervisor.models.ingress",
        SimpleNamespace(CreateSessionOptions=SimpleNamespace),
    )
    actions.client.ingress = SimpleNamespace(
        create_session=AsyncMock(return_value="private-session")
    )
    module = importlib.import_module("custom_components.ha_mcp_tools.native_app_tools")
    actions.binding = module
    actions.engine = Engine(
        actions.manager.backend, module.app_definitions(actions.manager)
    )
    actions.routes = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.app_routes"
    )
    actions.owner_module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.apps"
    )
    return actions


async def test_native_opaque_get_is_denied_by_read_only_before_any_connection(
    bound: Any,
) -> None:
    with pytest.raises(ToolFailure) as error:
        await bound.engine.call(
            "ha_manage_app",
            {"slug": "core_fixture", "path": "/api", "method": "GET"},
            CallContext("actor", read_only=True),
        )
    assert error.value.code == "READ_ONLY"
    bound.raw.send_command.assert_not_awaited()
    bound.client.ingress.create_session.assert_not_awaited()
    from custom_components.ha_mcp_tools.native_server_policy import (
        NativePolicy,
        filter_catalogue,
    )
    from ha_mcp._vendor.fastmcp.tools.base import Tool

    manager = SimpleNamespace(engine=bound.engine, policy=NativePolicy(read_only=True))
    projected = [
        Tool.from_function(
            lambda: None,
            name=definition.name,
            annotations=definition.annotations,
            tags=set(definition.tags),
        )
        for definition in bound.engine.tools.values()
    ]
    assert [tool.name for tool in filter_catalogue(manager, projected)] == [
        "ha_get_app"
    ]


async def test_native_opaque_get_is_admitted_as_write_through_engine(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    exchange = AsyncMock(
        return_value=(200, {"content-type": "application/json"}, {"value": 42})
    )
    monkeypatch.setattr(bound.owner_module, "http_exchange", exchange)
    result = await bound.engine.call(
        "ha_manage_app",
        {"slug": "core_fixture", "path": "/api", "method": "GET"},
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["response"] == {"value": 42}
    exchange.assert_awaited_once()


async def test_ingress_uses_actual_owner_url_and_actor_session_without_debug_credentials(
    bound: Any,
) -> None:
    route = await bound.routes.AppRoute.prepare(
        bound.service, "core_fixture", "/api?a=1", None, {"cookie": "wrong"}
    )
    await route.open()
    assert route.url.host == "supervisor"
    assert route.url.path == "/ingress/private-app-token/api"
    assert route.url.query_string == "a=1"
    options = bound.client.ingress.create_session.call_args.args[0]
    assert options.user_id == "actor"
    assert route.headers["Cookie"] == "ingress_session=private-session"
    assert "private" not in route.debug_url
    assert "a=1" not in route.debug_url


async def test_auth_revoked_during_ingress_creation_stops_before_app_request(
    bound: Any,
) -> None:
    async def revoke(options: Any) -> str:
        bound.manager.backend.authorize.side_effect = PermissionError("revoked")
        return "private-session"

    bound.client.ingress.create_session.side_effect = revoke
    route = await bound.routes.AppRoute.prepare(
        bound.service, "core_fixture", "/api", None, None
    )
    with pytest.raises(ToolFailure):
        await route.open()


async def test_array_patch_uses_one_get_one_post_and_keeps_before_image(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = [{"id": "a", "enabled": False}, {"id": "b", "enabled": True}]
    original = copy.deepcopy(before)
    calls = []

    async def exchange(route: Any, method: str, body: Any) -> Any:
        calls.append((method, copy.deepcopy(body)))
        return (
            200,
            {"content-type": "application/json"},
            before if method == "GET" else {"saved": True},
        )

    monkeypatch.setattr(bound.owner_module, "http_exchange", exchange)
    result = await bound.engine.call(
        "ha_manage_app",
        {
            "slug": "core_fixture",
            "path": "/flows",
            "array_patch": {
                "operations": [{"op": "patch", "id": "a", "patches": {"enabled": True}}]
            },
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert before == original
    assert [method for method, body in calls] == ["GET", "POST"]
    assert calls[1][1] == [{"id": "a", "enabled": True}, {"id": "b", "enabled": True}]
    assert result.data["items_before"] == result.data["items_after"] == 2


async def test_array_patch_does_not_apply_old_array_to_replaced_app(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exchange(route: Any, method: str, body: Any) -> Any:
        assert method == "GET", "A replacement app received an old array"
        bound.replies["/addons/core_fixture/info"]["version"] = "changed"
        return 200, {}, [{"id": "a"}]

    monkeypatch.setattr(bound.owner_module, "http_exchange", exchange)
    result = await bound.engine.call(
        "ha_manage_app",
        {
            "slug": "core_fixture",
            "path": "/flows",
            "array_patch": {"operations": [{"op": "delete", "id": "a"}]},
        },
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert result.status == "confirmed" and result.data["requires_reread"]
    assert result.data["prerequisite_get_completed"]
    assert result.data["array_patch_applied"] is False
    assert result.data["write_completed"] is False


@pytest.fixture
def app_responses(bound: Any) -> Any:
    """Exercise the real HTTP adapter with a controlled Core-owned response."""
    info = bound.replies["/addons/core_fixture/info"]
    info.update(ip_address="172.30.33.1", network={"1880/tcp": 1880})
    info["options"]["leave_front_door_open"] = False
    info["schema"].append({"name": "leave_front_door_open", "type": "boolean"})
    replies: list[tuple[int, bytes]] = []
    calls: list[str] = []

    @asynccontextmanager
    async def request(method: str, url: Any, **kwargs: Any) -> Any:
        calls.append(method)
        assert kwargs["allow_redirects"] is False
        status, body = replies.pop(0)

        async def chunks(size: int) -> Any:
            yield body

        yield SimpleNamespace(
            status=status,
            headers={
                "Content-Type": "application/json",
                "Set-Cookie": "private-session",
            },
            charset="utf-8",
            content=SimpleNamespace(iter_chunked=chunks),
        )

    bound.raw.websession.request = request
    bound.routes.ingress_view(bound.manager.hass)._websession.request = request
    return replies, calls


async def test_native_direct_rejection_preserves_safe_settings_and_auth_guidance(
    bound: Any, app_responses: Any
) -> None:
    replies, calls = app_responses
    replies.append((401, b'{"echo":"private-session private-app-token"}'))
    result = await bound.engine.call(
        "ha_manage_app",
        {"slug": "core_fixture", "path": "/flows", "port": 1880, "debug": True},
        CallContext("actor"),
    )
    assert result.is_error and result.data["status_code"] == 401
    assert result.status == "outcome_unknown"
    assert result.data["addon_config"]["options"]["leave_front_door_open"] is False
    hint = result.data["suggestion"].lower()
    assert all(
        word in hint
        for word in ("ingress", "security", "restart", "leave_front_door_open")
    )
    assert not any(
        secret in repr(result.data)
        for secret in ("fixture-secret", "private-session", "private-app-token")
    )
    assert calls == ["GET"]


async def test_native_ingress_post_rejection_retains_uncertainty_without_weakening_auth(
    bound: Any, app_responses: Any
) -> None:
    replies, calls = app_responses
    replies.append((401, b'{"echo":"private-session"}'))
    result = await bound.engine.call(
        "ha_manage_app",
        {
            "slug": "core_fixture",
            "path": "/api",
            "method": "POST",
            "body": {"save": True},
        },
        CallContext("actor"),
    )
    assert result.is_error and result.data["status_code"] == 401
    assert result.status == "outcome_unknown"
    assert "session" in result.data["suggestion"].lower()
    assert "addon_config" not in result.data
    assert "leave_front_door_open" not in repr(result.data)
    assert "private-session" not in repr(result.data)
    assert calls == ["POST"]


@pytest.mark.parametrize("rejected_method", ["GET", "POST"])
async def test_array_patch_never_accepts_an_http_error_as_a_before_image_or_saved_array(
    bound: Any, app_responses: Any, rejected_method: str
) -> None:
    replies, calls = app_responses
    if rejected_method == "POST":
        replies.append((200, b'[{"id":"a"}]'))
    # An app can return a list-shaped body even with a rejection status.
    replies.append((403, b'[{"id":"a"}]'))
    result = await bound.engine.call(
        "ha_manage_app",
        {
            "slug": "core_fixture",
            "path": "/flows",
            "array_patch": {"operations": [{"op": "delete", "id": "a"}]},
        },
        CallContext("actor"),
    )
    assert result.is_error and result.data["status_code"] == 403
    assert result.status == "outcome_unknown"
    assert result.data.get("array_patch_applied") is not True
    assert calls == (["GET"] if rejected_method == "GET" else ["GET", "POST"])
