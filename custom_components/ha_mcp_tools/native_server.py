"""Own a FastMCP lifespan on Home Assistant's loop, without another listener."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web
from homeassistant.core import HassJob
from homeassistant.util.aiohttp import MockRequest

from .const import (
    DATA_ADMIN_TOKEN,
    DATA_NATIVE_OWNER_ID,
    DATA_OAUTH_CLIENT_ID,
    DATA_SERVER_USER_ID,
    DATA_WEBHOOK_ID,
    OPT_NATIVE_HTTP_LIMIT,
    OPT_NATIVE_STREAM_SECONDS,
    SERVER_CONFIG_SUBDIR,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
)
from .http_adapter import serve_asgi, serve_buffered_asgi
from .native_server_policy import NativePolicy, catalogue_middleware, load_policy

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from ha_mcp.tool_api import CallContext, ToolResult

_LOGGER = logging.getLogger(__name__)
_MAX_BODY = 1_048_576


def _prewarm() -> None:
    """Resolve SDK imports and terminal caches outside HA's event loop."""
    importlib.import_module("ha_mcp.runtime.catalog")
    importlib.import_module(f"{__package__}.native_backend")
    importlib.import_module("ha_mcp._vendor.fastmcp")
    from io import StringIO

    from rich.console import Console

    from ha_mcp._vendor.fastmcp.server.dependencies import is_docket_available

    is_docket_available()
    console = Console(file=StringIO(), force_terminal=True, width=100)
    console.print("Native runtime · Préparation · 測試 🏠")
    try:
        raise RuntimeError("Private SDK traceback warmup")
    except RuntimeError:
        console.print_exception(show_locals=False)


def current_context() -> CallContext:
    """Only integration-supplied ASGI state can carry caller identity."""
    from ha_mcp._vendor.fastmcp.server.dependencies import get_http_request

    return cast("CallContext", get_http_request().scope["state"]["ha_mcp_context"])


def mcp_result(result: ToolResult) -> Any:
    """Keep payloads intact and report execution outcome in MCP metadata."""
    from ha_mcp._vendor.fastmcp.tools.base import ToolResult as MCPResult

    return MCPResult(
        content=json.dumps(result.data, default=str),
        structured_content=result.data if isinstance(result.data, dict) else None,
        is_error=result.is_error,
        meta={"ha_mcp": {"status": result.status, "operation_id": result.operation_id}},
    )


def _project_tool(manager: NativeServerManager, definition: Any) -> Any:
    """Project an existing engine definition, including its original schema."""
    from pydantic import PrivateAttr

    from ha_mcp._vendor.fastmcp.tools.base import Tool

    class RuntimeTool(Tool):
        _manager: Any = PrivateAttr()

        def __init__(self) -> None:
            super().__init__(
                name=definition.name,
                description=definition.description,
                parameters=definition.input_schema,
                output_schema=definition.output_schema,
                annotations=definition.annotations,
                tags=set(definition.tags),
            )
            self._manager = manager

        async def run(self, arguments: dict[str, Any]) -> Any:
            return mcp_result(
                await self._manager.engine.call(self.name, arguments, current_context())
            )

    return RuntimeTool()


class NativeServerManager:
    """One config entry owns the engine, lifespan and admitted HTTP requests."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.path = f"/api/webhook/{entry.data[DATA_WEBHOOK_ID]}"
        self.engine: Any = None
        self.backend: Any = None
        self.mcp: Any = None
        self.app: Any = None
        self.lifespan_state: dict[str, Any] = {}
        self.accepting = False
        self._ready = asyncio.Event()
        self._stop_requested = asyncio.Event()
        self._startup_error: BaseException | None = None
        self._supervisor: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._http_tasks: set[asyncio.Task] = set()
        self._stopped = False
        self.policy = NativePolicy()
        self.http_limit = max(
            1, min(256, int(entry.options.get(OPT_NATIVE_HTTP_LIMIT, 32)))
        )
        self.stream_seconds = max(
            30, min(3600, int(entry.options.get(OPT_NATIVE_STREAM_SECONDS, 900)))
        )

    @property
    def is_running(self) -> bool:
        """Whether ingress can admit work on the owned lifespan."""
        return (
            self.accepting
            and self._supervisor is not None
            and not self._supervisor.done()
        )

    async def async_start(self) -> None:
        """Start on HA's loop; enter and exit lifespan from one supervisor task."""
        await self.hass.async_add_executor_job(_prewarm)
        from ha_mcp._vendor.fastmcp import FastMCP
        from ha_mcp._vendor.mcp.server.request_state import RequestStateSecurity
        from ha_mcp.runtime import Engine
        from ha_mcp.runtime.catalog import build_initial_catalog

        from .native_backend import NativeBackend

        self.backend = NativeBackend(self.hass)
        self.engine = Engine(self.backend, tools=build_initial_catalog(self.backend))
        self.mcp = FastMCP(
            name="Home Assistant MCP",
            instructions="Use Home Assistant tools with the caller's permissions. Preserve errors and uncertain operation outcomes; do not retry an uncertain write automatically.",
            request_state_security=RequestStateSecurity(
                keys=[secrets.token_bytes(32)],
                ttl=300,
                audience=f"ha-mcp:{self.entry.entry_id}",
                bind_principal=lambda _ctx: json.dumps(
                    [current_context().user_id, current_context().client_id]
                ),
            ),
        )
        for definition in self.engine.tools.values():
            self.mcp.add_tool(_project_tool(self, definition))
        from .native_llm_import import register_import_tools

        register_import_tools(self)
        self.mcp.add_middleware(catalogue_middleware(self))
        await self.refresh_policy()
        self.app = self.mcp.http_app(
            path=self.path, stateless_http=True, host_origin_protection=False
        )
        self._supervisor = self.hass.async_create_background_task(
            self._supervise(), "ha-mcp-native-lifespan"
        )
        try:
            async with asyncio.timeout(30):
                await self._ready.wait()
            if self._startup_error is not None:
                raise self._startup_error
            self.accepting = True
            self.entry.async_on_unload(
                self.hass.async_add_shutdown_job(HassJob(self.async_stop))
            )
        except BaseException:
            await self.async_stop()
            raise

    async def _supervise(self) -> None:
        try:
            async with self.app.lifespan(self.app) as state:
                self.lifespan_state = dict(state or {})
                self._ready.set()
                await self._stop_requested.wait()
        except BaseException as err:
            self._startup_error = err
            self._ready.set()
            raise
        finally:
            self.accepting = False

    async def refresh_policy(self) -> None:
        """Apply all-client restrictions independently from Assist exposure."""
        policy = await self.hass.async_add_executor_job(
            load_policy, Path(self.hass.config.path(SERVER_CONFIG_SUBDIR))
        )
        self.policy = policy
        self.engine.read_only = policy.read_only
        names = {*self.engine.tools, "ha_native_llm_tools", "ha_native_llm_call"}
        self.engine.enabled_tools = {name for name in names if policy.enabled(name)}

    async def async_stop(self) -> None:
        """Cancellation of an unload caller never cancels owned cleanup."""
        if self._cleanup_task is None or (
            self._cleanup_task.done() and not self._stopped
        ):
            self._cleanup_task = asyncio.create_task(
                self._cleanup(), name="ha-mcp-native-cleanup"
            )
        await asyncio.shield(self._cleanup_task)

    async def _cleanup(self) -> None:
        self.accepting = False
        if self.engine is not None:
            await self.engine.stop()
        tasks = tuple(self._http_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stop_requested.set()
        if self._supervisor is not None:
            try:
                async with asyncio.timeout(10):
                    await asyncio.shield(self._supervisor)
            except TimeoutError:
                self._supervisor.cancel()
                await asyncio.gather(self._supervisor, return_exceptions=True)
            except Exception:
                _LOGGER.exception("Native MCP lifespan ended with an error")
        self._stopped = True

    async def _owner_id(self) -> str:
        """Migrate an existing credential's owner, without issuing a new token."""
        owner = self.entry.data.get(DATA_NATIVE_OWNER_ID)
        if owner:
            return str(owner)
        token = self.entry.data.get(DATA_ADMIN_TOKEN)
        if token:
            refresh = self.hass.auth.async_validate_access_token(str(token))
            if inspect.isawaitable(refresh):
                refresh = await refresh
            owner = refresh.user.id if refresh is not None else None
        else:
            owner = self.entry.data.get(DATA_SERVER_USER_ID)
        if not owner:
            raise PermissionError(
                "Configure a Home Assistant owner for this capability"
            )
        user = await self.hass.auth.async_get_user(owner)
        if (
            user is None
            or not user.is_active
            or not user.is_admin
            or user.system_generated
        ):
            raise PermissionError("The configured capability owner is unavailable")
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, DATA_NATIVE_OWNER_ID: owner}
        )
        return str(owner)

    async def request_context(
        self, request: web.Request, cfg: dict[str, Any], *, read_only: bool
    ) -> CallContext:
        """Bind the validated credential to a concrete user and client identity."""
        from ha_mcp.tool_api import CallContext

        if cfg["auth_mode"] == WEBHOOK_AUTH_HA:
            token = request.headers.get("Authorization", "")[7:].strip()
            refresh = self.hass.auth.async_validate_access_token(token)
            if inspect.isawaitable(refresh):
                refresh = await refresh
            if refresh is None:
                raise PermissionError("Invalid Home Assistant credential")
            user_id = refresh.user.id
            client_id = str(refresh.client_id or refresh.id)

            async def validate() -> None:
                actual = self.hass.auth.async_validate_access_token(token)
                if inspect.isawaitable(actual):
                    actual = await actual
                if (
                    actual is None
                    or actual.user.id != user_id
                    or str(actual.client_id or actual.id) != client_id
                ):
                    raise PermissionError(
                        "Home Assistant credential was revoked or expired"
                    )
        else:
            user_id = await self._owner_id()
            client_id = (
                str(self.entry.data.get(DATA_OAUTH_CLIENT_ID))
                if cfg["auth_mode"] == WEBHOOK_AUTH_LEGACY
                else f"webhook:{self.entry.entry_id}"
            )

            async def validate() -> None:
                from .const import DATA_WEBHOOK, DOMAIN
                from .mcp_webhook import _check_webhook_auth

                if (
                    not self.accepting
                    or self.hass.data.get(DOMAIN, {}).get(DATA_WEBHOOK) is not cfg
                ):
                    raise PermissionError("Webhook capability is no longer active")
                if (
                    await self._owner_id() != user_id
                    or await _check_webhook_auth(request, cfg) is not None
                ):
                    raise PermissionError("Webhook capability was revoked")

        return CallContext(
            user_id=user_id,
            client_id=client_id,
            credential=validate,
            read_only=read_only,
        )

    async def async_handle_http(
        self, request: web.Request, cfg: dict[str, Any], *, read_only: bool = False
    ) -> web.StreamResponse:
        """Admit bounded concurrent streams; long SSE has its own generous budget."""
        if not self.is_running:
            return web.Response(status=503, text="MCP server is not available")
        if len(self._http_tasks) >= self.http_limit:
            return web.Response(
                status=429,
                text="MCP request capacity reached",
                headers={"Retry-After": "1"},
            )
        task = asyncio.current_task()
        assert task is not None
        self._http_tasks.add(task)
        try:
            context = await self.request_context(request, cfg, read_only=read_only)
            await self.backend.authorize(context)
            await self.refresh_policy()
            state = {**self.lifespan_state, "ha_mcp_context": context}
            if isinstance(request, MockRequest):
                return await self._cloudhook(request, state)
            async with asyncio.timeout(self.stream_seconds):
                return await serve_asgi(
                    self.app,
                    request,
                    request_state=state,
                    path=self.path,
                    max_body_bytes=_MAX_BODY,
                )
        except PermissionError:
            return web.Response(status=403, text="MCP caller is not authorized")
        finally:
            self._http_tasks.discard(task)

    async def _cloudhook(self, request: Any, state: dict[str, Any]) -> web.Response:
        from .cloudhook import REPLY_SECONDS

        try:
            async with asyncio.timeout(REPLY_SECONDS):
                body = await request.content.read(_MAX_BODY + 1)
                if len(body) > _MAX_BODY:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=_MAX_BODY, actual_size=len(body)
                    )
                return await serve_buffered_asgi(
                    self.app,
                    method=request.method,
                    path=self.path,
                    headers=[
                        (str(k).lower().encode("latin-1"), str(v).encode("latin-1"))
                        for k, v in request.headers.items()
                    ],
                    body=body,
                    state=state,
                )
        except TimeoutError:
            return web.Response(
                status=504,
                text="Streaming MCP replies cannot be relayed through a cloudhook",
            )


def native_panel_response(
    manager: NativeServerManager, method: str, path: str
) -> web.Response:
    """The existing authenticated panel exposes native runtime status."""
    if not manager.is_running:
        return web.Response(status=503, text="The MCP server is not running")
    if method != "GET":
        return web.Response(status=405, text="Use the integration's Configure screen")
    status: dict[str, Any] = {
        "runtime": "native",
        "running": True,
        "tools": sorted(manager.engine.tools),
        "active_requests": len(manager._http_tasks),
    }
    if path.startswith("api/"):
        return web.json_response(status)
    from html import escape

    names = "".join(f"<li>{escape(name)}</li>" for name in status["tools"])
    return web.Response(
        content_type="text/html",
        text=(
            "<!doctype html><html><meta charset=utf-8><title>HA-MCP</title>"
            "<body><h1>HA-MCP · Native runtime</h1>"
            "<p>Running inside Home Assistant. Configure access and Assist exposure "
            "in Settings → Devices &amp; services → HA-MCP → Configure.</p>"
            f"<h2>Available tools</h2><ul>{names}</ul></body></html>"
        ),
    )
