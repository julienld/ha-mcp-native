"""Instance-owned device IDs, separate from Engine invocation IDs and credentials."""

import asyncio
import copy
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from ha_mcp.runtime.device_status import bulk_status_result, operation_status_result
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.operation_manager import DeviceOperation, OperationStatus

from .registry_access import Authorize, authorize_actor


@dataclass(slots=True)
class _Operation:
    owner: tuple[str | None, str | None]
    operation: DeviceOperation = field(repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    terminal_at: float | None = None
    failure: ToolFailure | None = field(default=None, repr=False)
    final_states: dict[str, Any] = field(default_factory=dict, repr=False)


class NativeDeviceOperations:
    def __init__(self, *, max_entries: int = 1000, retention: float = 300) -> None:
        self.entries: dict[str, _Operation] = {}
        self.max_entries = max_entries
        self.retention = retention
        self.closed = False

    def _cleanup(self) -> None:
        deadline = time.monotonic() - self.retention
        self.entries = {
            key: entry
            for key, entry in self.entries.items()
            if entry.terminal_at is None or entry.terminal_at >= deadline
        }

    def reserve(
        self,
        actor: CallContext,
        operations: list[dict[str, Any]],
        rows: list[dict[str, Any]],
    ) -> list[str]:
        self._cleanup()
        if self.closed or len(self.entries) + len(rows) > self.max_entries:
            raise ToolFailure(
                "OWNED_CAPACITY",
                "Device-operation tracking is unavailable or full; no new batch was started.",
                status="not_started",
            )
        if len(operations) != len(rows):
            raise ValueError("Each operation requires exactly one resolved service row")
        reserved = {}
        for operation, row in zip(operations, rows, strict=True):
            identifier = f"device-{uuid.uuid4().hex}"
            reserved[identifier] = _Operation(
                (actor.user_id, actor.client_id),
                DeviceOperation(
                    operation_id=identifier,
                    entity_id=operation["entity_id"],
                    action=operation["action"],
                    service_domain=row["domain"],
                    service_name=row["service"],
                    service_data=copy.deepcopy(row["service_data"]),
                    expected_state={"state": row["expected_state"]}
                    if row.get("expected_state") is not None
                    else None,
                    timeout_ms=1000
                    * min(float(operation.get("timeout_seconds", 10)), 60),
                ),
            )
        self.entries.update(reserved)
        return list(reserved)

    def finish(self, identifier: str, result: dict[str, Any]) -> None:
        entry = self.entries[identifier]
        if entry.done.is_set():
            raise RuntimeError("A device-operation outcome cannot be rewritten")
        operation = entry.operation
        operation.completion_time = time.time() * 1000
        if result.get("confirmed") and result.get("outcome") != "outcome_unknown":
            operation.status = OperationStatus.COMPLETED
            entry.final_states = {
                transition["entity_id"]: copy.deepcopy(transition["new_state"])
                for transition in result.get("transitions", [])
            }
            operation.result_state = entry.final_states.get(operation.entity_id)
            if operation.result_state is None and len(entry.final_states) == 1:
                operation.result_state = next(iter(entry.final_states.values()))
        else:
            operation.status = OperationStatus.FAILED
            unknown = bool(result.get("attempted"))
            failure = ToolFailure(
                "OUTCOME_UNKNOWN"
                if unknown
                else result.get("error_code", "SERVICE_CALL_FAILED"),
                (
                    "The device action was attempted but its final state was not confirmed. Inspect the entity before deciding on another action."
                    if unknown
                    else str(result.get("error") or "The device action was not started")
                ),
                # This read has completed; outcome refers to the earlier action.
                status="confirmed",
            )
            failure.data.update(
                operation_id=identifier,
                entity_id=operation.entity_id,
                action=operation.action,
                operation_outcome="outcome_unknown" if unknown else "not_started",
                attempted=unknown,
            )
            entry.failure = failure
        entry.terminal_at = time.monotonic()
        entry.done.set()

    def _get(self, actor: CallContext, identifier: str) -> _Operation:
        self._cleanup()
        entry = self.entries.get(identifier)
        if entry is None or entry.owner != (actor.user_id, actor.client_id):
            failure = ToolFailure(
                "RESOURCE_NOT_FOUND",
                "Operation not found or expired.",
                status="confirmed",
            )
            failure.data["operation_id"] = identifier
            raise failure
        return entry

    async def status(
        self,
        actor: CallContext,
        authorize: Authorize,
        operation_id: str | list[str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        await authorize_actor(actor, authorize)
        if isinstance(operation_id, str):
            return await self._one(actor, authorize, operation_id, timeout_seconds)
        if not operation_id:
            raise ToolFailure(
                "VALIDATION_MISSING_PARAMETER", "No operation IDs provided."
            )

        async def read(identifier: str) -> dict[str, Any]:
            try:
                return await self._one(actor, authorize, identifier, timeout_seconds)
            except ToolFailure as error:
                if error.code == "AUTH_FORBIDDEN":
                    raise
                return {
                    **copy.deepcopy(error.data),
                    "operation_id": identifier,
                    "status": "not_found"
                    if error.code == "RESOURCE_NOT_FOUND"
                    else "failed",
                }

        # These waiters only wait on shared Events, never own a service dispatch.
        # Cancel/drain our waiters on a failed authorization; Events stay intact.
        tasks = [asyncio.create_task(read(identifier)) for identifier in operation_id]
        try:
            statuses = await asyncio.gather(*tasks)
            await authorize_actor(actor, authorize)
            return bulk_status_result(operation_id, statuses)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                from ha_mcp.utils.owned_executor import await_owned_io

                await await_owned_io(asyncio.gather(*tasks, return_exceptions=True))

    async def _one(
        self, actor: CallContext, authorize: Authorize, identifier: str, timeout: float
    ) -> dict[str, Any]:
        entry = self._get(actor, identifier)
        if not entry.done.is_set() and timeout > 0:
            with suppress(TimeoutError):
                async with asyncio.timeout(timeout):
                    await entry.done.wait()
        await authorize_actor(actor, authorize)
        if self._get(actor, identifier) is not entry:
            raise ToolFailure("RESOURCE_NOT_FOUND", "Operation expired while waiting.")
        if entry.failure is not None:
            error = entry.failure
            raise ToolFailure(
                error.code,
                str(error),
                data=copy.deepcopy(error.data),
                status=error.status,
            )
        result = copy.deepcopy(
            operation_status_result(
                entry.operation,
                verification_method="core_state_change",
                monitoring="core_state_changes",
            )
        )
        if len(entry.final_states) > 1:
            result["final_states"] = copy.deepcopy(entry.final_states)
        return result

    def close(self) -> None:
        self.closed = True
        for identifier, entry in self.entries.items():
            if not entry.done.is_set():
                self.finish(
                    identifier, {"attempted": True, "outcome": "outcome_unknown"}
                )
