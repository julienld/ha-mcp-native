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
        )

    async def call_owned(
        self,
        name: str,
        invoke: Callable[[], Awaitable[Any]],
        context: CallContext,
        *,
        writes: bool,
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
            self._run(operation_id, name, invoke, context, writes),
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
    ) -> ToolResult:
        record = self.operations[operation_id]
        try:
            await self.backend.authorize(context)
            self._check_policy(name, context, writes)
            record["status"] = "executing"
            async with asyncio.timeout(self.operation_timeout):
                raw = await invoke()
            result = raw if isinstance(raw, ToolResult) else ToolResult(raw)
            status = result.status or ("failed" if result.is_error else "confirmed")
            record["status"] = status
            return replace(result, status=status, operation_id=operation_id)
        except asyncio.CancelledError:
            record["status"] = self._failure_status(record, writes)
            raise
        except Exception as exc:
            status = self._failure_status(record, writes)
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
            return ToolResult(
                data, is_error=True, status=status, operation_id=operation_id
            )

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
