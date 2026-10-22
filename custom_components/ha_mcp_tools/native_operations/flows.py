"""Owned Core configuration flows with explicit, revalidated continuations."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, NoReturn

from homeassistant.core import HomeAssistant

from ha_mcp.errors import HomeAssistantAPIError
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

_LOGGER = logging.getLogger(__name__)
FLOW_OPERATIONS = frozenset(
    {"config.flow.start", "config.flow.configure", "config.flow.abort"}
)


def _unknown_flow_outcome(error: Exception) -> NoReturn:
    raise ToolFailure(
        "OUTCOME_UNKNOWN",
        "Core flow processing failed after dispatch; its effects are unknown. Inspect the integration before another write.",
        status="outcome_unknown",
    ) from error


@dataclass(slots=True)
class OwnedFlow:
    kind: str
    user_id: str | None
    client_id: str
    touched: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    entry_lock: asyncio.Lock | None = None


def _prepare_flow_result(
    hass: HomeAssistant, kind: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Use Core's exact serializer, including its removal of entry credentials.

    Verified against Core 2026.10.2: these pure result formatters do not use a
    request or execute a view handler. They serialize the original flow schema
    through Core's serializer and project CREATE_ENTRY to its public entry.
    """
    from homeassistant.components.config.config_entries import (
        ConfigManagerFlowIndexView,
        OptionManagerFlowIndexView,
        SubentryManagerFlowIndexView,
    )

    views = {
        "config": ConfigManagerFlowIndexView,
        "options": OptionManagerFlowIndexView,
        "subentry": SubentryManagerFlowIndexView,
    }
    managers = {
        "config": hass.config_entries.flow,
        "options": hass.config_entries.options,
        "subentry": hass.config_entries.subentries,
    }
    prepared: dict[str, Any] = views[kind](managers[kind])._prepare_result_json(result)
    return prepared


class NativeFlows:
    """Only this backend's flows can be resumed, expired or aborted by it."""

    def __init__(
        self,
        hass: HomeAssistant,
        authorize: Callable[[CallContext], Awaitable[None]],
        *,
        max_flows: int = 32,
        ttl: float = 900,
    ) -> None:
        self.hass = hass
        self.authorize = authorize
        self.max_flows = max_flows
        self.ttl = ttl
        self.owned: dict[str, OwnedFlow] = {}
        self.starting = 0

    def _manager(self, kind: str) -> Any:
        attribute = {
            "config": "flow",
            "options": "options",
            "subentry": "subentries",
        }.get(kind)
        if attribute is None:
            raise UnsupportedOperation(f"config.flow.kind.{kind}")
        return getattr(self.hass.config_entries, attribute)

    async def execute(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> Any:
        if operation == "config.flow.abort":
            return await self.abort_owned(arguments["flow_id"], context)
        if context.read_only:
            raise ToolFailure(
                "READ_ONLY",
                "Configuration flows can write state.",
                status="not_started",
            )
        self._expire()
        if operation == "config.flow.start":
            return await self._start(arguments, context)
        flow_id = arguments["flow_id"]
        owned = self.owned.get(flow_id)
        if owned is None or (owned.user_id, owned.client_id) != (
            context.user_id,
            context.client_id,
        ):
            raise ToolFailure(
                "FLOW_NOT_OWNED",
                "This caller does not own this flow.",
                status="not_started",
            )
        async with owned.lock:
            await self._admit_flow(context)
            if self.owned.get(flow_id) is not owned:
                raise ToolFailure(
                    "FLOW_NOT_OWNED",
                    "This flow has already completed.",
                    status="not_started",
                )
            owned.touched = time.monotonic()
            if operation != "config.flow.configure":
                raise UnsupportedOperation(operation)
            return await await_owned_io(
                self._configure_owned(
                    owned, flow_id, arguments.get("user_input"), context
                )
            )

    async def abort_owned(self, flow_id: str, context: CallContext) -> dict[str, str]:
        """Release only this caller's pending flow, even after admission revocation."""
        owned = self.owned.get(flow_id)
        if owned is None or (owned.user_id, owned.client_id) != (
            context.user_id,
            context.client_id,
        ):
            raise ToolFailure(
                "FLOW_NOT_OWNED",
                "This caller does not own this flow.",
                status="not_started",
            )
        async with owned.lock:
            if self.owned.get(flow_id) is owned:
                self._abort(flow_id, owned)
        return {"message": "Flow aborted"}

    async def _configure_owned(
        self, owned: OwnedFlow, flow_id: str, user_input: Any, context: CallContext
    ) -> dict[str, Any]:
        await self._admit_flow(context)
        result = await self._configure(owned.kind, flow_id, user_input)
        return self._result(result, owned)

    async def _configure(self, kind: str, flow_id: str, user_input: Any) -> Any:
        from homeassistant.data_entry_flow import InvalidData, UnknownFlow

        try:
            return await self._manager(kind).async_configure(flow_id, user_input)
        except InvalidData as error:
            # Same public rejection payload as Core's FlowManagerResourceView.
            raise HomeAssistantAPIError(
                "Invalid flow input",
                status_code=400,
                response_data={"errors": error.schema_errors},
            ) from error
        except UnknownFlow as error:
            raise HomeAssistantAPIError(
                "Invalid flow specified", status_code=404
            ) from error
        except Exception as error:  # noqa: BLE001 - dispatch has already occurred
            _unknown_flow_outcome(error)

    async def _start(
        self, arguments: dict[str, Any], context: CallContext
    ) -> dict[str, Any]:
        if len(self.owned) + self.starting >= self.max_flows:
            raise ToolFailure(
                "FLOW_LIMIT",
                "Too many active configuration flows.",
                status="not_started",
            )
        kind = arguments.get("kind", "config")
        manager = self._manager(kind)
        handler = arguments["handler"]
        flow_context: dict[str, Any] = {}
        if kind in {"config", "subentry"}:
            flow_context["source"] = "user"
            selector = "entry_id" if kind == "config" else "subentry_id"
            if arguments.get(selector) is not None:
                flow_context.update(
                    source="reconfigure", **{selector: arguments[selector]}
                )
        if kind == "subentry":
            handler = tuple(handler)
        owned = OwnedFlow(kind, context.user_id, context.client_id)
        entry_id = (
            handler
            if kind == "options"
            else handler[0]
            if kind == "subentry"
            else arguments.get("entry_id")
        )
        self.starting += 1
        try:
            if entry_id is not None:
                entry_lock = registry_update_lock("config_entry", entry_id)
                # Nothing has been dispatched: keep this acquisition cancellable
                # so stop can drain a task waiting on somebody else's flow.
                await entry_lock.acquire()
                owned.entry_lock = entry_lock
            return await await_owned_io(
                self._initialize_owned(
                    manager,
                    handler,
                    flow_context,
                    kind,
                    context,
                    arguments.get("expected_modified_at"),
                    owned,
                )
            )
        except BaseException:
            if not any(row is owned for row in self.owned.values()):
                self._release_entry(owned)
            raise
        finally:
            self.starting -= 1

    async def _initialize_owned(
        self,
        manager: Any,
        handler: Any,
        flow_context: dict[str, Any],
        kind: str,
        context: CallContext,
        expected: Any,
        owned: OwnedFlow,
    ) -> dict[str, Any]:
        await self._admit_flow(context)
        if expected is not None:
            entry_id = (
                handler
                if kind == "options"
                else handler[0]
                if kind == "subentry"
                else flow_context.get("entry_id")
            )
            if entry_id is None:
                raise UnsupportedOperation(
                    "config.flow.expected_revision_requires_entry"
                )
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.modified_at.timestamp() != expected:
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The config entry changed after its snapshot was read.",
                    status="not_started",
                )
        result = await self._init(manager, handler, flow_context)
        return self._result(result, owned)

    async def _admit_flow(self, context: CallContext) -> None:
        try:
            await self.authorize(context)
            check_mutation_admission(context)
        except PermissionError as error:
            raise ToolFailure(
                "AUTH_REQUIRED", "Flow admission was revoked.", status="not_started"
            ) from error
        except ToolFailure as error:
            raise ToolFailure(
                error.code, str(error), data=error.data, status="not_started"
            ) from error

    async def _init(self, manager: Any, handler: Any, context: dict[str, Any]) -> Any:
        from homeassistant.data_entry_flow import UnknownHandler, UnknownStep

        try:
            return await manager.async_init(handler, context=context)
        except UnknownHandler as error:
            raise HomeAssistantAPIError(
                "Invalid handler specified", status_code=404
            ) from error
        except UnknownStep as error:
            raise HomeAssistantAPIError(str(error), status_code=400) from error
        except Exception as error:  # noqa: BLE001 - dispatch has already occurred
            _unknown_flow_outcome(error)

    def _result(self, result: dict[str, Any], owned: OwnedFlow) -> dict[str, Any]:
        flow_id = result.get("flow_id")
        if result.get("type") in {"create_entry", "abort"}:
            if flow_id:
                self.owned.pop(flow_id, None)
            self._release_entry(owned)
        elif flow_id:
            self.owned[flow_id] = owned
        else:
            self._release_entry(owned)
            raise ToolFailure(
                "INVALID_FLOW_RESULT",
                "Core returned a pending flow without an identity.",
                status="outcome_unknown",
            )
        try:
            return _prepare_flow_result(self.hass, owned.kind, result)
        except Exception as error:  # noqa: BLE001 - dispatch has already occurred
            _unknown_flow_outcome(error)

    def _abort(self, flow_id: str, owned: OwnedFlow) -> None:
        from homeassistant.data_entry_flow import UnknownFlow

        try:
            self._manager(owned.kind).async_abort(flow_id)
        except UnknownFlow:
            _LOGGER.debug("Owned flow already completed: %s", flow_id)
        self.owned.pop(flow_id, None)
        self._release_entry(owned)

    @staticmethod
    def _release_entry(owned: OwnedFlow) -> None:
        if owned.entry_lock is not None:
            owned.entry_lock.release()
            owned.entry_lock = None

    def _expire(self) -> None:
        now = time.monotonic()
        for flow_id, owned in list(self.owned.items()):
            if not owned.lock.locked() and now - owned.touched >= self.ttl:
                self._abort(flow_id, owned)

    async def stop(self) -> None:
        for flow_id, owned in list(self.owned.items()):
            self._abort(flow_id, owned)
