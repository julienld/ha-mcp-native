"""Entry-owned Monty execution over the common engine and saved-script workflow."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

from ha_mcp.runtime.code_persistence import SavedToolsPersistence
from ha_mcp.runtime.code_registry import SavedToolsRegistry
from ha_mcp.runtime.code_runner import run_sandboxed_code
from ha_mcp.runtime.code_session import CodeSession
from ha_mcp.runtime.code_validation import BLOCKED_TOOLS, _sandbox_error
from ha_mcp.runtime.mutation_guard import check_mutation_admission, current_admission
from ha_mcp.tool_api import (
    CallContext,
    ToolDefinition,
    ToolFailure,
    ToolResult,
    UnsupportedOperation,
)
from ha_mcp.utils.owned_executor import await_owned_io

from .native_code_prepare import prepare_native_code
from .native_server_policy import environment_name

_LOGGER = logging.getLogger(__name__)
_NAME = "ha_manage_custom_tool"
_LIMITS = {
    "code_mode_max_duration": float,
    "code_mode_max_memory": int,
    "code_mode_max_recursion": int,
    "code_mode_max_invocations": int,
    "code_mode_saved_tools_path": str,
}


class NativeCode:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.lock = asyncio.Lock()
        self.registry: SavedToolsRegistry | None = None
        self.settings: Any = None
        self.monty: Any = None
        self.limits: Any = None

    def _initialize(self) -> bool:
        from ha_mcp.config_registry import SETTING_BOUNDS
        from ha_mcp.config_settings import Settings

        try:
            from pydantic_monty import Monty, ResourceLimits
        except ImportError:
            _LOGGER.warning("Native code mode needs the pydantic-monty dependency")
            return False
        values = {}
        for name, kind in _LIMITS.items():
            values[name] = kind(
                os.environ.get(
                    environment_name(name),
                    self.manager.policy.flags.get(
                        name, Settings.model_fields[name].default
                    ),
                )
            )
            if name in SETTING_BOUNDS:
                low, high = SETTING_BOUNDS[name]
                if not low <= values[name] <= high:
                    raise ValueError(f"{name} is outside supported bounds")
        self.settings = SimpleNamespace(**values)
        persistence = SavedToolsPersistence()
        self.registry = SavedToolsRegistry(
            persistence.load(self.settings.code_mode_saved_tools_path), persistence.save
        )
        self.monty, self.limits = Monty, ResourceLimits
        return True

    async def async_definition(self) -> ToolDefinition | None:
        from ha_mcp.runtime.catalog import define_tool
        from ha_mcp.tools.tool_hints import write_hints

        if not (
            self.manager.policy.flag("enable_beta_features")
            and self.manager.policy.flag("enable_code_mode")
        ):
            return None
        if not await self.manager.settings_ui._io(self._initialize):
            return None

        async def invoke(arguments: dict[str, Any], actor: CallContext) -> Any:
            return await self.run(arguments, actor)

        definition = define_tool(
            self.manager.backend,
            CodeSession,
            _NAME,
            handler=invoke,
            annotations=write_hints(
                "Custom Tool", destructive=True, idempotent=False, open_world=False
            ),
            tags=frozenset({"System", "beta"}),
            source_module="tools_code",
            meta={
                "ha_mcp": {
                    "native_support": {
                        "partial": True,
                        "note": "Native code mode supports call_tool, delete_saved_tool and bounded native mappings for api_get, api_post and ws_send. GET config returns Core's configuration body under ha_get_system_health admission; states and services use ha_get_state and ha_list_services admission. POST services/events invoke ha_call_service/ha_call_event; template rendering uses ha_eval_template admission and Core's text renderer. Raw helpers are unavailable while entity visibility hiding is enforced. Unported endpoints and commands are refused before dispatch; use call_tool or the legacy runtime. Nested approvals return pending errors without automatically replaying the script.",
                    }
                }
            },
        )
        return replace(definition, operation_timeout=self._operation_timeout)

    def _operation_timeout(self, arguments: dict[str, Any]) -> float | None:
        if arguments.get("code") is not None or arguments.get("run_saved") is not None:
            return float(self.settings.code_mode_max_duration) + 120
        return None

    async def _registry_call(
        self, method: str, *args: Any, actor: CallContext | None = None
    ) -> Any:
        assert self.registry is not None
        async with self.lock:
            if actor is not None:
                await self.manager.refresh_policy()
                await self._authorize(actor, writes=True)
            # Hold the registry lock until issued I/O actually ends, including
            # when stop cancels the operation's waiter.
            return await await_owned_io(
                self.manager.settings_ui._io(getattr(self.registry, method), *args)
            )

    def _check_enabled(self) -> None:
        if not self.manager.policy.flag(
            "enable_beta_features"
        ) or not self.manager.policy.flag("enable_code_mode"):
            raise ToolFailure(
                "FEATURE_DISABLED", "Native code mode is disabled", status="not_started"
            )

    async def _authorize(self, actor: CallContext, *, writes: bool = False) -> None:
        await self.manager.backend.authorize(actor)
        self._check_enabled()
        if writes:
            check_mutation_admission(actor)
            if actor.read_only or self.manager.policy.read_only:
                raise ToolFailure(
                    "READ_ONLY",
                    "Saved-script mutations are disabled",
                    status="not_started",
                )

    async def run(self, arguments: dict[str, Any], actor: CallContext) -> ToolResult:
        await self._authorize(actor)
        if self.registry is None:
            raise UnsupportedOperation("code.run")
        bridge = _NativeBridge(self, actor)

        async def list_saved() -> dict[str, Any]:
            return cast(dict[str, Any], await self._registry_call("list"))

        async def get_saved(name: str) -> dict[str, str] | None:
            rows = await list_saved()
            return cast(dict[str, str] | None, rows["data"]["saved_tools"].get(name))

        async def save_tool(
            response: dict[str, Any], name: str, code: str, justification: str
        ) -> None:
            await self._registry_call(
                "save_as",
                response,
                name,
                code,
                justification,
                self.settings.code_mode_saved_tools_path,
                actor=actor,
            )

        async def run_code(code: str) -> Any:
            from .native_code_api import NativeCodeAPI

            api = NativeCodeAPI(bridge)
            code = await self.manager.settings_ui._io(prepare_native_code, code)
            return await run_sandboxed_code(
                code,
                {
                    "call_tool": bridge.call_tool,
                    "delete_saved_tool": bridge.delete_saved_tool,
                    "api_get": api.api_get,
                    "api_post": api.api_post,
                    "ws_send": api.ws_send,
                },
                self.settings,
                self.monty,
                self.limits,
            )

        session = CodeSession(
            list_saved=list_saved,
            get_saved=get_saved,
            save_tool=save_tool,
            run_code=run_code,
        )
        failure: ToolFailure | None = None
        try:
            response = await session.ha_manage_custom_tool(**arguments)
        except (ToolFailure, PermissionError) as error:
            failure = (
                error
                if isinstance(error, ToolFailure)
                else ToolFailure("AUTH_FORBIDDEN", str(error), status="not_started")
            )
            response = {}
        return await self._finish_run(actor, bridge, response, failure)

    async def _finish_run(
        self,
        actor: CallContext,
        bridge: _NativeBridge,
        response: dict[str, Any],
        failure: ToolFailure | None,
    ) -> ToolResult:
        registry_changed = bridge.registry_changed or bool(
            response.get("data", {}).get("saved_as")
        )
        try:
            await self._authorize(actor)
        except (PermissionError, ToolFailure) as authority_failure:
            code = (
                authority_failure.code
                if isinstance(authority_failure, ToolFailure)
                else "AUTH_FORBIDDEN"
            )
            changed = registry_changed or any(
                item["status"] not in {"not_started", "pending"}
                for item in bridge.outcomes
            )
            raise ToolFailure(
                code,
                "Code results were withheld because the caller or code-mode authority changed; inspect prior operations before retrying.",
                data={
                    "success": False,
                    "error": {
                        "code": code,
                        "message": "Code results withheld after authority changed",
                    },
                    "saved_registry_changed": registry_changed,
                    "operations": bridge.outcomes,
                },
                status="outcome_unknown" if changed else "not_started",
            ) from authority_failure
        if failure is not None:
            raise ToolFailure(
                failure.code,
                str(failure),
                data={
                    **failure.data,
                    "operations": bridge.outcomes,
                    "saved_registry_changed": registry_changed,
                },
                status="outcome_unknown"
                if registry_changed or bridge.may_have_acted
                else failure.status,
            ) from failure
        if bridge.outcomes:
            response["data"]["operations"] = bridge.outcomes
        uncertain = any(item["status"] == "outcome_unknown" for item in bridge.outcomes)
        return ToolResult(
            response,
            is_error=uncertain,
            status="outcome_unknown" if uncertain else None,
        )


class _NativeBridge:
    def __init__(self, owner: NativeCode, actor: CallContext) -> None:
        self.owner, self.actor = owner, actor
        self._parent_admission = current_admission.get()
        self._child_actor = replace(actor, tool_guard=self._guard_child)
        self.count = 0
        self.outcomes: list[dict[str, Any]] = []
        self.registry_changed = False
        self.may_have_acted = False

    def _guard_child(self, name: str) -> None:
        # Child admission replaces the active ContextVar, so retain the
        # script's live guard through child authorization/approval waits too.
        self.owner._check_enabled()
        if self._parent_admission is not None:
            self._parent_admission.check_policy()
        if self.actor.tool_guard is not None:
            self.actor.tool_guard(name)

    def _limit(self) -> dict[str, Any] | None:
        from ha_mcp.errors import ErrorCode

        self.count += 1
        if self.count > self.owner.settings.code_mode_max_invocations:
            return _sandbox_error(
                ErrorCode.VALIDATION_FAILED, "Sandbox invocation limit exceeded"
            )
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        from ha_mcp.errors import ErrorCode
        from ha_mcp.renamed_tools import current_tool_name

        if error := self._limit():
            return error
        if current_tool_name(name) in BLOCKED_TOOLS:
            return _sandbox_error(
                ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS,
                f"Tool '{name}' cannot be called from sandbox code",
            )
        return await self._invoke_child(name, arguments)

    async def _raw_boundary(self) -> None:
        visibility = getattr(self.owner.manager, "visibility", None)
        if visibility is not None:
            await visibility.before(
                "ha_manage_custom_tool",
                {"code": "native raw API helper"},
                self._child_actor,
            )

    async def _invoke_child(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        operation: str | None = None,
        payload: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        from .native_approvals import MCP_APPROVAL_CONTEXT

        # A nested approval must not seal the outer script as a replayable
        # continuation: earlier script calls may already have changed HA.
        token = MCP_APPROVAL_CONTEXT.set(None)
        try:
            await self.owner.manager.refresh_policy()
            await self.owner._authorize(self.actor, writes=True)
            if raw:
                await self._raw_boundary()
            if operation is None:
                result = await self.owner.manager.engine.call(
                    name, arguments, self._child_actor
                )
            else:

                async def invoke() -> Any:
                    await self._raw_boundary()
                    result = await self.owner.manager.backend.execute(
                        operation, payload or {}, self._child_actor
                    )
                    await self._raw_boundary()
                    return result

                if name not in self.owner.manager.engine.tools:
                    raise ToolFailure(
                        "UNKNOWN_TOOL", f"Unknown tool {name!r}.", status="not_started"
                    )
                result = await self.owner.manager.engine.call_owned(
                    name, invoke, self._child_actor, writes=False, arguments=arguments
                )
        except ToolFailure as failure:
            return failure.data
        finally:
            MCP_APPROVAL_CONTEXT.reset(token)
        record = self.owner.manager.engine.operations.get(result.operation_id or "", {})
        self.may_have_acted |= result.status == "outcome_unknown" or (
            record.get("writes", True)
            and result.status not in {"not_started", "pending"}
        )
        self.outcomes.append(
            {"tool": name, "status": result.status, "operation_id": result.operation_id}
        )
        return result.data

    async def delete_saved_tool(self, name: Any) -> dict[str, Any]:
        if error := self._limit():
            return error
        result = cast(
            dict[str, Any],
            await self.owner._registry_call(
                "delete",
                name,
                self.owner.settings.code_mode_saved_tools_path,
                actor=self.actor,
            ),
        )
        self.registry_changed |= bool(result.get("deleted"))
        return result
