"""Bounded operation owner, independent of the lifetime of an HTTP request."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from typing import Any
from uuid import uuid4

from ha_mcp.tool_api import (
    Backend,
    CallContext,
    PolicyGuard,
    ResultFilter,
    ToolDefinition,
    ToolFailure,
    ToolResult,
)

_LOGGER = logging.getLogger(__name__)


class Engine:
    """A single admission path; a disconnected waiter cannot cancel a write."""

    def __init__(
        self,
        backend: Backend,
        tools: Iterable[ToolDefinition] = (),
        *,
        read_only: bool = False,
        max_operations: int = 32,
        operation_timeout: float = 120,
        drain_timeout: float = 10,
        cancel_timeout: float = 1,
        max_records: int = 128,
        policy_guard: PolicyGuard | None = None,
        result_filter: ResultFilter | None = None,
    ) -> None:
        if min(max_operations, operation_timeout, max_records) <= 0:
            raise ValueError("Operation limits must be positive")
        if min(drain_timeout, cancel_timeout) < 0:
            raise ValueError("Drain limits must be nonnegative")
        self.backend = backend
        self.tools: dict[str, ToolDefinition] = {}
        for definition in tools:
            self.register(definition)
        self.read_only = read_only
        self.enabled_tools: set[str] | None = None
        self.accepting = True
        self.max_operations = max_operations
        self.operation_timeout = operation_timeout
        self.drain_timeout = drain_timeout
        self.cancel_timeout = cancel_timeout
        self.max_records = max_records
        self.policy_guard = policy_guard
        self.result_filter = result_filter
        self.tasks: set[asyncio.Task[ToolResult]] = set()
        self.operations: dict[str, dict[str, Any]] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self.tools:
            raise ValueError(f"Duplicate tool: {definition.name}")
        self.tools[definition.name] = definition

    def _check_policy(self, name: str, context: CallContext, writes: bool) -> None:
        if not self.accepting:
            raise ToolFailure("INTEGRATION_STOPPING", "The integration is stopping.")
        for allowed in (self.enabled_tools, context.allowed_tools):
            if allowed is not None and name not in allowed:
                raise ToolFailure("TOOL_DISABLED", f"Tool {name!r} is not enabled.")
        if writes and (self.read_only or context.read_only):
            raise ToolFailure("READ_ONLY", "Writes are disabled for this caller.")

    async def call(
        self, name: str, arguments: dict[str, Any], context: CallContext
    ) -> ToolResult:
        await self.backend.authorize(context)
        definition = self.tools.get(name)
        if definition is None:
            raise ToolFailure("UNKNOWN_TOOL", f"Unknown tool {name!r}.")
        self._check_policy(name, context, not definition.read_only)
        if not isinstance(arguments, dict):
            raise ToolFailure("INVALID_ARGUMENTS", "Tool arguments must be an object.")
        validated = (
            definition.validate(arguments) if definition.validate else dict(arguments)
        )
        return await self.call_owned(
            name,
            lambda: definition.handler(validated, context),
            context,
            writes=not definition.read_only,
            arguments=validated,
        )

    async def call_owned(
        self,
        name: str,
        invoke: Callable[[], Awaitable[Any]],
        context: CallContext,
        *,
        writes: bool,
        arguments: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Use for imported tools too; never automatically retry an invocation."""
        await self.backend.authorize(context)
        self._check_policy(name, context, writes)
        if len(self.tasks) >= self.max_operations:
            raise ToolFailure("OPERATION_LIMIT", "Too many in-flight operations.")
        operation_id = uuid4().hex
        self.operations[operation_id] = {
            "tool": name,
            "user_id": context.user_id,
            "client_id": context.client_id,
            "status": "admitted",
            "writes": writes,
        }
        task = asyncio.create_task(
            self._run(operation_id, name, invoke, context, writes, arguments or {}),
            name=f"ha-mcp-operation-{operation_id}",
        )
        self.tasks.add(task)
        task.add_done_callback(self._operation_done)
        return await asyncio.shield(task)

    def _operation_done(self, task: asyncio.Task[ToolResult]) -> None:
        self.tasks.discard(task)
        # Retrieve exceptions even if the HTTP waiter disconnected meanwhile.
        if not task.cancelled():
            task.exception()
        self._prune_records()

    async def _run(
        self,
        operation_id: str,
        name: str,
        invoke: Callable[[], Awaitable[Any]],
        context: CallContext,
        writes: bool,
        arguments: dict[str, Any],
    ) -> ToolResult:
        record = self.operations[operation_id]
        deadline = asyncio.get_running_loop().time() + self.operation_timeout
        try:
            async with asyncio.timeout_at(deadline):
                raw = await self._invoke_authorized(
                    name, arguments, context, writes, record, invoke
                )
            result = raw if isinstance(raw, ToolResult) else ToolResult(raw)
            status = result.status or ("failed" if result.is_error else "confirmed")
            record["status"] = status
            result = replace(result, status=status, operation_id=operation_id)
        except asyncio.CancelledError:
            record["status"] = self._failure_status(record, writes)
            raise
        except Exception as exc:
            status = self._failure_status(record, writes)
            if isinstance(exc, ToolFailure) and exc.status is not None:
                status = exc.status
            record["status"] = status
            if isinstance(exc, ToolFailure):
                data = exc.data
            else:
                _LOGGER.exception("Tool %s failed (%s)", name, operation_id)
                data = {
                    "success": False,
                    "error": {
                        "code": "OUTCOME_UNKNOWN"
                        if status == "outcome_unknown"
                        else "EXECUTION_FAILED",
                        "message": type(exc).__name__,
                    },
                }
            result = ToolResult(
                data, is_error=True, status=status, operation_id=operation_id
            )
        return await self._filter_result(name, arguments, context, result, deadline)

    async def _filter_result(
        self,
        name: str,
        arguments: dict[str, Any],
        context: CallContext,
        result: ToolResult,
        deadline: float,
    ) -> ToolResult:
        if self.result_filter is None:
            return result
        try:
            async with asyncio.timeout_at(deadline):
                filtered = await self.result_filter(name, arguments, context, result)
            return replace(
                filtered, status=result.status, operation_id=result.operation_id
            )
        except Exception:
            _LOGGER.exception(
                "Result filtering failed for %s (%s)", name, result.operation_id
            )
            return replace(
                result,
                data={
                    "success": False,
                    "error": {
                        "code": "RESULT_FILTER_FAILED",
                        "message": "The operation result could not be safely returned.",
                    },
                },
                is_error=True,
            )

    async def _invoke_authorized(
        self,
        name: str,
        arguments: dict[str, Any],
        context: CallContext,
        writes: bool,
        record: dict[str, Any],
        invoke: Callable[[], Awaitable[Any]],
    ) -> Any:
        await self.backend.authorize(context)
        self._check_policy(name, context, writes)
        if self.policy_guard is not None:
            guarded = await self.policy_guard(name, arguments, context, writes)
            if guarded is not None:
                return replace(guarded, status=guarded.status or "not_started")
            await self.backend.authorize(context)
            self._check_policy(name, context, writes)
        record["status"] = "executing"
        return await invoke()

    @staticmethod
    def _failure_status(record: dict[str, Any], writes: bool) -> str:
        if record["status"] == "admitted":
            return "not_started"
        return "outcome_unknown" if writes else "failed"

    def _prune_records(self) -> None:
        for key, record in list(self.operations.items()):
            if len(self.operations) <= self.max_records:
                break
            if record["status"] not in {"admitted", "executing"}:
                del self.operations[key]

    async def stop(self) -> None:
        """Stop admission and drain; a failed drain can be attempted again."""
        self.accepting = False
        if not self.tasks:
            return
        _, pending = await asyncio.wait(set(self.tasks), timeout=self.drain_timeout)
        for task in pending:
            task.cancel()
        if pending:
            _, survivors = await asyncio.wait(pending, timeout=self.cancel_timeout)
            if survivors:
                raise RuntimeError("owned_operations_not_drained")
