"""Own a FastMCP lifespan on Home Assistant's loop, without another listener."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
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
from .native_server_policy import (
    NativePolicy,
    catalogue_middleware,
    catalogue_transform,
    load_policy,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from ha_mcp.tool_api import CallContext, ToolResult

_LOGGER = logging.getLogger(__name__)
_MAX_BODY = 1_048_576
_INSTRUCTIONS = "Use Home Assistant tools with the caller's permissions. Preserve errors and uncertain operation outcomes; do not retry an uncertain write automatically."


def _prewarm() -> bool:
    """Resolve SDK imports and terminal caches outside HA's event loop."""
    importlib.import_module("ha_mcp.runtime.catalog")
    importlib.import_module(f"{__package__}.native_backend")
    importlib.import_module("ha_mcp._vendor.fastmcp")
    importlib.import_module("ha_mcp.settings_ui")
    importlib.import_module("ha_mcp.policy.handlers")
    importlib.import_module(f"{__package__}.native_visibility")
    from io import StringIO

    from ha_mcp._vendor.fastmcp.server.dependencies import is_docket_available
    from ha_mcp._vendor.rich.console import Console

    is_docket_available()
    console = Console(file=StringIO(), force_terminal=True, width=100)
    console.print("Native runtime · Préparation · 測試 🏠")
    try:
        raise RuntimeError("Private SDK traceback warmup")
    except RuntimeError:
        console.print_exception(show_locals=False)
    from ha_mcp.utils.skill_loader import best_practices_skill, get_skills_dir

    return best_practices_skill(get_skills_dir()) is not None


def current_context() -> CallContext:
    """Only integration-supplied ASGI state can carry caller identity."""
    from ha_mcp._vendor.fastmcp.server.dependencies import get_http_request

    return cast("CallContext", get_http_request().scope["state"]["ha_mcp_context"])


def mcp_result(result: ToolResult, *, allow_continuation: bool = True) -> Any:
    """Keep payloads intact and report execution outcome in MCP metadata."""
    from ha_mcp._vendor.fastmcp.tools.base import ToolResult as MCPResult

    from .native_approvals import project_continuation

    if (
        allow_continuation
        and (continuation := project_continuation(result)) is not None
    ):
        return continuation

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
            support = definition.meta.get("ha_mcp", {}).get("native_support", {})
            note = support.get("note", "")
            description = definition.description + (
                f"\n\nNative support: {note}" if note else ""
            )
            super().__init__(
                name=definition.name,
                description=description,
                parameters=definition.input_schema,
                output_schema=definition.output_schema,
                annotations=definition.annotations,
                tags=set(definition.tags),
                meta=definition.meta,
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
        self.approval_gate: Any = None
        self.settings_ui: Any = None
        self.skill_available = False
        self.acknowledgment: Any = None
        self.search_enabled = False
        self.snapshots: Any = None
        self.visibility: Any = None
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
        self.skill_available = await self.hass.async_add_executor_job(_prewarm)
        from ha_mcp._vendor.fastmcp import FastMCP
        from ha_mcp._vendor.mcp.server.request_state import RequestStateSecurity
        from ha_mcp.runtime import Engine
        from ha_mcp.runtime.best_practices import AcknowledgmentState
        from ha_mcp.runtime.catalog import build_initial_catalog
        from ha_mcp.runtime.skills import SkillGuideReader
        from ha_mcp.tools.renamed_tool_middleware import RenamedToolAliasMiddleware

        from .native_approvals import NativeApprovalGate, approval_context_middleware
        from .native_backend import NativeBackend
        from .native_settings import NativeSettings
        from .native_snapshots import NativeSnapshots
        from .native_visibility import NativeVisibility

        self.acknowledgment = AcknowledgmentState()
        self.snapshots = NativeSnapshots(self)
        self.backend = NativeBackend(
            self.hass,
            service_policy=self.service_policy,
            skill_guide_reader=SkillGuideReader(ack_line=self.skill_ack_line),
            skill_content_enabled=lambda: self.policy.flag("enable_mandatory_bps"),
            helper_backup=self.snapshots.helper_backup,
        )
        self.settings_ui = NativeSettings(self)
        self.visibility = NativeVisibility(self)
        self.approval_gate = NativeApprovalGate(self)
        self.engine = Engine(
            self.backend,
            tools=build_initial_catalog(self.backend),
            policy_guard=self.approval_gate,
            result_filter=self.visibility.after,
        )
        self.mcp = FastMCP(
            name="Home Assistant MCP",
            instructions=_INSTRUCTIONS,
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
        self.mcp.add_middleware(RenamedToolAliasMiddleware())
        self.mcp.add_middleware(catalogue_middleware(self))
        self.mcp.add_middleware(approval_context_middleware())
        await self.refresh_policy()
        self.mcp.add_transform(catalogue_transform(self))
        self._configure_search()
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
            self.approval_gate.start_decisions()
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

    def _configure_search(self) -> None:
        from ha_mcp.config_registry import SETTING_BOUNDS
        from ha_mcp.config_settings import Settings
        from ha_mcp.transforms import CategorizedSearchTransform

        self.search_enabled = self.policy.flag("enable_tool_search")
        if not self.search_enabled:
            return
        default = Settings.model_fields["tool_search_max_results"].default
        maximum = int(
            os.environ.get(
                "TOOL_SEARCH_MAX_RESULTS",
                self.policy.flags.get("tool_search_max_results", default),
            )
        )
        low, high = SETTING_BOUNDS["tool_search_max_results"]
        if not low <= maximum <= high:
            raise ValueError("tool_search_max_results is outside supported bounds")
        self.mcp.add_transform(
            CategorizedSearchTransform(
                max_results=maximum, always_visible=sorted(self.policy.pinned)
            )
        )

    async def refresh_policy(self) -> None:
        """Apply all-client restrictions independently from Assist exposure."""
        policy = await self.hass.async_add_executor_job(
            load_policy, Path(self.hass.config.path(SERVER_CONFIG_SUBDIR))
        )
        self.policy = policy
        self.engine.read_only = policy.read_only
        names = {*self.engine.tools, "ha_native_llm_tools", "ha_native_llm_call"}
        self.engine.enabled_tools = {
            name
            for name in names
            if (
                policy.tool_enabled(self.engine.tools[name])
                if name in self.engine.tools
                else policy.enabled(name)
            )
        }
        self.mcp.instructions = _INSTRUCTIONS + (
            "\n\n" + policy.custom_instructions if policy.custom_instructions else ""
        )

    def service_policy(self, domain: str, service: str) -> None:
        from ha_mcp.runtime.service_policy import check_service_policy

        check_service_policy(
            domain,
            service,
            enable_snapshot_actions=self.policy.flag("enable_snapshot_actions"),
            backup_read_only=self.policy.flag("backup_read_only"),
        )

    def strict_best_practices(self) -> bool:
        return (
            self.skill_available
            and self.policy.flag("enable_mandatory_bps")
            and self.policy.flag("enable_strict_mandatory_bps")
        )

    def skill_ack_line(self) -> str | None:
        return self.acknowledgment.line() if self.strict_best_practices() else None

    def check_best_practices(
        self, name: str, arguments: dict[str, Any]
    ) -> ToolResult | None:
        from ha_mcp.runtime.best_practices import (
            STRICT_BPS_GATED_TOOLS,
            STRICT_BPS_KEY_PARAM,
            required_acknowledgment_error,
        )
        from ha_mcp.tool_api import ToolResult

        if name not in STRICT_BPS_GATED_TOOLS:
            return None
        supplied = arguments.pop(STRICT_BPS_KEY_PARAM, None)
        if self.strict_best_practices() and not self.acknowledgment.accepts(supplied):
            return ToolResult(
                required_acknowledgment_error(name), is_error=True, status="not_started"
            )
        return None

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
        if self.approval_gate is not None:
            await self.approval_gate.stop()
        if (
            self.backend is not None
            and (stop_backend := getattr(self.backend, "stop", None)) is not None
        ):
            await stop_backend()
        if self.snapshots is not None:
            await self.snapshots.async_stop()
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
