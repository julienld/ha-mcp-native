"""Bounded Core batch dispatch with admission and explicit attempted outcomes."""

import asyncio
import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.bulk_selection import MAX_SELECTOR_ENTITIES
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from ..core_call_service import _guard_call_service_target
from ..websocket_api.bulk import (
    _build_bulk_op_result,
    _bulk_match_immediate,
    _bulk_op_record,
    _bulk_register_all,
    _bulk_wait_all,
)
from .registry_access import (
    Authorize,
    admit_registry_write,
    authorize_actor,
    registry_failure,
)


@dataclass(slots=True)
class NativeBulkDispatch:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)
    service_policy: Callable[[str, str], None] = field(kw_only=True, repr=False)
    concurrency: int = 4

    async def execute(
        self,
        operations: list[dict[str, Any]],
        *,
        parallel: bool,
        timeout: float,
    ) -> dict[str, Any]:
        if not 0 < len(operations) <= MAX_SELECTOR_ENTITIES:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                f"A native batch requires 1 to {MAX_SELECTOR_ENTITIES} operations.",
                status="not_started",
            )
        if not 0 <= timeout <= 60 or self.concurrency < 1:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Invalid batch budget.",
                status="not_started",
            )
        # Retain this call and its listeners until every started owner has drained,
        # even if a caller disconnects or cancels it repeatedly.
        targets = [
            target for operation in operations for target in operation["entity_ids"]
        ]
        copied = copy.deepcopy(operations)
        work = (
            self._ordered(copied, timeout)
            if len(targets) != len(set(targets))
            else self._execute(copied, parallel=parallel, timeout=timeout)
        )
        return await await_owned_io(work)

    async def _ordered(
        self, operations: list[dict[str, Any]], timeout: float
    ) -> dict[str, Any]:
        # Overlapping targets cannot share preimages/listeners: a first toggle
        # would otherwise confirm the following toggle before it even starts.
        try:
            await admit_registry_write(self.actor, self.authorize)
            for operation in operations:
                self._guard(operation)
        except Exception as error:
            raise registry_failure(error, {"operation": "bulk preflight"}) from error
        deadline = asyncio.get_running_loop().time() + timeout
        values: list[dict[str, Any]] = []
        stopped = False
        for operation in operations:
            if stopped:
                value = self._not_started(
                    operation, "Not attempted after an earlier batch failure"
                )
            else:
                try:
                    response = await self._execute(
                        [operation],
                        parallel=False,
                        timeout=max(0, deadline - asyncio.get_running_loop().time()),
                    )
                    value = response["operations"][0]
                except ToolFailure as error:
                    if error.code == "AUTH_FORBIDDEN":
                        attempted = sum(row["attempted"] for row in values)
                        failure = ToolFailure(
                            error.code,
                            str(error),
                            status="outcome_unknown"
                            if attempted or error.status == "outcome_unknown"
                            else "not_started",
                        )
                        failure.data["attempted_operations"] = (
                            attempted + error.data.get("attempted_operations", 0)
                        )
                        raise failure from error
                    value = self._not_started(operation, str(error))
                    value["error_code"] = error.code
                stopped = bool(value.get("error"))
            values.append(value)
        return {**self._assemble(values), "serialized_duplicate_targets": True}

    @staticmethod
    def _not_started(operation: dict[str, Any], message: str) -> dict[str, Any]:
        return {
            "domain": operation["domain"],
            "service": operation["service"],
            "entity_ids": operation["entity_ids"],
            "attempted": False,
            "dispatched": False,
            "confirmed": False,
            "partial": False,
            "transitions": [],
            "outcome": "not_started",
            "error": message,
        }

    def _guard(self, operation: dict[str, Any]) -> None:
        self.service_policy(operation["domain"], operation["service"])
        _guard_call_service_target(self.hass, operation["domain"], operation["service"])

    async def _execute(
        self, operations: list[dict[str, Any]], *, parallel: bool, timeout: float
    ) -> dict[str, Any]:
        try:
            await admit_registry_write(self.actor, self.authorize)
            for operation in operations:
                self._guard(operation)
        except Exception as error:
            raise registry_failure(error, {"operation": "bulk preflight"}) from error
        records = [
            {
                **_bulk_op_record(self.hass, operation, wait=timeout > 0),
                "context": Context(user_id=self.actor.user_id),
                "attempted": False,
                "outcome": "not_started",
            }
            for operation in operations
        ]
        unsubs = _bulk_register_all(self.hass, records)
        try:
            await self._dispatch_all(records, parallel)
            _bulk_match_immediate(self.hass, records)
            await _bulk_wait_all(records, timeout)
            result = self._result(records)
        finally:
            # Each listener is independently owned; one unexpected cleanup error
            # must not strand the rest after a dispatched operation.
            for unsubscribe in unsubs:
                try:
                    unsubscribe()
                except Exception:
                    import logging

                    logging.getLogger(__name__).exception(
                        "Bulk listener cleanup failed"
                    )
        try:
            await authorize_actor(self.actor, self.authorize)
        except Exception as error:
            attempted = sum(record["attempted"] for record in records)
            failure = ToolFailure(
                error.code if isinstance(error, ToolFailure) else "AUTH_FORBIDDEN",
                "Caller admission changed during the batch. Dispatched actions were not repeated.",
                status="outcome_unknown" if attempted else "not_started",
            )
            failure.data["attempted_operations"] = attempted
            raise failure from error
        return result

    async def _dispatch_all(
        self, records: list[dict[str, Any]], parallel: bool
    ) -> None:
        stopped = asyncio.Event()
        semaphore = asyncio.Semaphore(self.concurrency if parallel else 1)

        async def dispatch(record: dict[str, Any]) -> None:
            async with semaphore:
                if stopped.is_set():
                    record["error"] = "Not attempted after an earlier batch failure"
                    return
                try:
                    await admit_registry_write(self.actor, self.authorize)
                    if stopped.is_set():
                        record["error"] = "Not attempted after an earlier batch failure"
                        return
                    self._guard(record)
                    record["attempted"] = True
                    record["outcome"] = "outcome_unknown"
                    record["response"] = await self.hass.services.async_call(
                        record["domain"],
                        record["service"],
                        dict(record["service_data"]),
                        blocking=True,
                        return_response=False,
                        context=record["context"],
                    )
                    record["dispatched"] = True
                    record["outcome"] = "confirmed"
                except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - classify failures at the actual dispatch boundary
                    # A provider may apply an action before raising any exception,
                    # including cancellation. Neither readback nor a retry proves
                    # the absence of its other effects.
                    record["error"] = f"{type(error).__name__}: {error}"
                    record["error_code"] = (
                        "OUTCOME_UNKNOWN"
                        if record["attempted"]
                        else getattr(error, "code", "SERVICE_CALL_FAILED")
                    )
                    stopped.set()

        await asyncio.gather(*(dispatch(record) for record in records))

    def _result(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        values = []
        for record in records:
            try:
                value = _build_bulk_op_result(self.hass, record)
                # Core values (dates, sets, proxies) are detached on its loop.
                value = json.loads(json_bytes(value))
            except Exception:  # noqa: BLE001 - post-dispatch projection cannot erase an attempted action
                value = {
                    "domain": record["domain"],
                    "service": record["service"],
                    "entity_ids": record["entity_ids"],
                    "dispatched": record["dispatched"],
                    "confirmed": False,
                    "partial": record["attempted"],
                    "transitions": [],
                    "error": "The action's final state could not be projected",
                }
            value.update(attempted=record["attempted"], outcome=record["outcome"])
            if record.get("error_code"):
                value["error_code"] = record["error_code"]
            values.append(value)
        return self._assemble(values)

    @staticmethod
    def _assemble(values: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "operations": values,
            "total": len(values),
            "dispatched": sum(value["dispatched"] for value in values),
            "attempted": sum(value["attempted"] for value in values),
            "failed": sum(bool(value.get("error")) for value in values),
        }
