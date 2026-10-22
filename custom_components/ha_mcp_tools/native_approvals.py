"""One approval gate for native MCP and Assist, using the shared policy model."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from ha_mcp.policy.approval_queue import (
    ApprovalQueue,
    PendingApproval,
    compute_args_hash,
)
from ha_mcp.policy.evaluator import (
    Verdict,
    evaluate,
    find_matching_rule,
    has_dynamic_selector_targets,
    normalize_stringified_containers,
)
from ha_mcp.policy.events import APPROVAL_REQUESTED_EVENT, build_requested_payload
from ha_mcp.policy.model import Policy
from ha_mcp.runtime.best_practices import STRICT_BPS_KEY_PARAM
from ha_mcp.tool_api import CallContext, ToolResult

# An actual FastMCP middleware context exists only on the MCP surface. Assist
# carries no SDK context, and uses the same queue through ordinary tool results.
MCP_APPROVAL_CONTEXT: ContextVar[Any | None] = ContextVar(
    "native_mcp_approval_context", default=None
)


def _policy_hash(policy: Policy) -> str:
    return compute_args_hash(
        {
            "rule_effect": policy.rule_effect,
            "rules": [rule.model_dump() for rule in policy.rules],
        }
    )


def _result(
    status: str, message: str, token: str | None = None, continuation: str | None = None
) -> ToolResult:
    data: dict[str, Any] = {
        "success": False,
        "error": {"code": "USER_APPROVAL_REQUIRED", "message": message},
    }
    if token is not None:
        data["approval_token"] = token
    if continuation is not None:
        data["approval_continuation"] = continuation
    return ToolResult(data, is_error=True, status=status)


class NativeApprovalGate:
    """The engine invokes this gate before marking an operation executing."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.queue = ApprovalQueue()
        self._unsubscribe: Any = None
        self._event_tasks: set[asyncio.Task] = set()

    def start_decisions(self) -> None:
        from ha_mcp.policy.decisions import (
            APPROVAL_RESPONSE_EVENT,
            ApprovalResponseListener,
        )
        from ha_mcp.policy.events import APPROVAL_RESULT_EVENT, build_result_payload

        def policy_provider() -> Policy:
            from .native_server_policy import load_policy

            return (
                load_policy(self.manager.settings_ui.directory).security_policy
                or Policy()
            )

        async def emit_result(token: str, decision: str, **kwargs: Any) -> None:
            self.manager.hass.bus.async_fire(
                APPROVAL_RESULT_EVENT, build_result_payload(token, decision, **kwargs)
            )

        listener = ApprovalResponseListener(
            policy_provider=policy_provider,
            queue=self.queue,
            data_dir=self.manager.settings_ui.directory,
            emit_result=emit_result,
        )

        async def on_event(event: Any) -> None:
            task = asyncio.current_task()
            assert task is not None
            self._event_tasks.add(task)
            try:
                await listener.process_event({"data": event.data})
            finally:
                self._event_tasks.discard(task)

        self._unsubscribe = self.manager.hass.bus.async_listen(
            APPROVAL_RESPONSE_EVENT, on_event
        )

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        tasks = tuple(self._event_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def __call__(
        self, name: str, arguments: dict[str, Any], actor: CallContext, writes: bool
    ) -> ToolResult | None:
        await self.manager.refresh_policy()
        await self._visibility_before(name, arguments, actor)
        receipt = arguments.get(STRICT_BPS_KEY_PARAM)
        if (failure := self._preflight(name, arguments)) is not None:
            return failure
        policy = self.manager.policy.security_policy
        if policy is None:
            return None
        args = normalize_stringified_containers(arguments)
        policy_hash = _policy_hash(policy)
        digest = compute_args_hash(
            {
                "principal": [actor.user_id, actor.client_id],
                "policy": policy_hash,
                "arguments": args,
            }
        )
        pending, continuation = self._resume(name, digest, policy)
        if evaluate(name, args, policy) != Verdict.REQUIRE_APPROVAL:
            return None
        dynamic = has_dynamic_selector_targets(name, args)
        remembered = not dynamic and policy.rule_effect == "require_approval"
        if pending is None and remembered and self.queue.is_remembered(name, digest):
            return None
        if pending is None:
            pending = (
                self.queue.create(
                    name, digest, args, ttl_minutes=policy.approval_ttl_minutes
                )
                if dynamic
                else await self.queue.find_or_create(
                    name, digest, args, ttl_minutes=policy.approval_ttl_minutes
                )
            )
        if continuation is None:
            continuation = self._continuation(pending, policy, dynamic)
        self._announce(pending, name, args, policy, actor, dynamic)
        wait = (
            continuation.wait_seconds()
            if continuation is not None
            else policy.wait_seconds
        )
        try:
            async with asyncio.timeout(wait):
                await pending.wait()
        except TimeoutError:
            return self._pending_result(pending, continuation, dynamic)
        await self.manager.refresh_policy()
        await self._visibility_before(name, arguments, actor)
        if (
            failure := self._preflight(
                name, {**arguments, STRICT_BPS_KEY_PARAM: receipt}
            )
        ) is not None:
            self.queue.remove(pending.token)
            return failure
        current = self.manager.policy.security_policy
        if current is None or _policy_hash(current) != policy_hash:
            self.queue.remove(pending.token)
            return _result(
                "not_started", "The approval policy changed. Issue a new call."
            )
        return self._consume(pending, name, args, policy, remembered)

    def _preflight(self, name: str, arguments: dict[str, Any]) -> ToolResult | None:
        preflight: Callable[[str, dict[str, Any]], ToolResult | None] | None = getattr(
            self.manager, "check_best_practices", None
        )
        return preflight(name, arguments) if preflight is not None else None

    async def _visibility_before(
        self, name: str, arguments: dict[str, Any], actor: CallContext
    ) -> None:
        visibility = getattr(self.manager, "visibility", None)
        if visibility is not None:
            await visibility.before(name, arguments, actor)

    def _resume(self, name: str, digest: str, policy: Policy) -> tuple[Any, Any]:
        context = MCP_APPROVAL_CONTEXT.get()
        if context is None:
            return None, None
        from ha_mcp.policy.mrtr import ApprovalContinuation

        resumed = ApprovalContinuation.resume(context, self.queue, name, digest, policy)
        return (resumed[1], resumed[0]) if resumed else (None, None)

    def _continuation(
        self, pending: PendingApproval, policy: Policy, dynamic: bool
    ) -> Any:
        context = MCP_APPROVAL_CONTEXT.get()
        if context is None:
            return None
        from ha_mcp.policy.mrtr import ApprovalContinuation, supports_mrtr

        if not supports_mrtr(context):
            return None
        return ApprovalContinuation.start(
            pending, policy, policy.wait_seconds, dynamic_targets=dynamic
        )

    def _pending_result(
        self, pending: PendingApproval, continuation: Any, dynamic: bool
    ) -> ToolResult:
        if continuation is not None:
            result = continuation.next_result(self.queue)
            if result is not None:
                # The neutral engine never imports an SDK type. Its caller
                # projects this sealed-reference payload onto MCP MRTR.
                state = continuation.model_copy(
                    update={"rounds": continuation.rounds + 1}
                ).model_dump_json()
                return _result(
                    "pending", "Awaiting administrator approval", pending.token, state
                )
        if dynamic:
            self.queue.remove(pending.token)
        return _result(
            "pending",
            "Awaiting administrator approval; no tool was executed",
            pending.token,
        )

    def _consume(
        self,
        pending: PendingApproval,
        name: str,
        args: dict[str, Any],
        policy: Policy,
        remembered: bool,
    ) -> ToolResult | None:
        if self.queue.get(pending.token) is not pending:
            return _result("not_started", "Approval expired or was removed")
        if pending.decision != "approved":
            self.queue.remove(pending.token)
            return _result("denied", "The administrator denied this call")
        rule = find_matching_rule(name, args, policy)
        remember = rule.remember_minutes if rule is not None and remembered else 0
        if not self.queue.consume_and_maybe_remember(
            pending, remember_minutes=remember
        ):
            return _result("not_started", "This approval has already been used")
        return None

    def _announce(
        self,
        pending: PendingApproval,
        name: str,
        args: dict[str, Any],
        policy: Policy,
        actor: CallContext,
        dynamic: bool,
    ) -> None:
        if not pending.mark_notified():
            return
        payload = build_requested_payload(
            pending, find_matching_rule(name, args, policy), single_use=dynamic
        )
        payload["user_id"] = actor.user_id
        payload["client_id"] = actor.client_id
        self.manager.hass.bus.async_fire(APPROVAL_REQUESTED_EVENT, payload)


def project_continuation(result: ToolResult) -> Any | None:
    """Only the actual MCP surface turns a pending result into requestState."""
    if result.status != "pending" or not isinstance(result.data, dict):
        return None
    state = result.data.get("approval_continuation")
    if not isinstance(state, str) or MCP_APPROVAL_CONTEXT.get() is None:
        return None
    from ha_mcp._vendor.fastmcp.tools.base import InputRequiredToolResult
    from ha_mcp._vendor.mcp_types import InputRequiredResult

    return InputRequiredToolResult(InputRequiredResult(request_state=state))


def approval_context_middleware() -> Any:
    from ha_mcp._vendor.fastmcp.server.middleware import Middleware

    class ApprovalContext(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            token = MCP_APPROVAL_CONTEXT.set(context)
            try:
                return await call_next(context)
            finally:
                MCP_APPROVAL_CONTEXT.reset(token)

    return ApprovalContext()
