"""Owned Core configuration flows with explicit, revalidated continuations."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

_LOGGER = logging.getLogger(__name__)
FLOW_OPERATIONS = frozenset(
    {"config.flow.start", "config.flow.configure", "config.flow.abort"}
)


@dataclass(slots=True)
class OwnedFlow:
    kind: str
    user_id: str | None
    client_id: str
    touched: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
            await self.authorize(context)
            if self.owned.get(flow_id) is not owned:
                raise ToolFailure(
                    "FLOW_NOT_OWNED",
                    "This flow has already completed.",
                    status="not_started",
                )
            owned.touched = time.monotonic()
            if operation == "config.flow.abort":
                self._abort(flow_id, owned)
                return {"message": "Flow aborted"}
            if operation != "config.flow.configure":
                raise UnsupportedOperation(operation)
            result = await self._manager(owned.kind).async_configure(
                flow_id, arguments.get("user_input")
            )
            return self._result(result, owned)

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
        self.starting += 1
        try:
            result = await manager.async_init(handler, context=flow_context)
            return self._result(
                result, OwnedFlow(kind, context.user_id, context.client_id)
            )
        finally:
            self.starting -= 1

    def _result(self, result: dict[str, Any], owned: OwnedFlow) -> dict[str, Any]:
        flow_id = result.get("flow_id")
        if flow_id:
            if result.get("type") in {"create_entry", "abort"}:
                self.owned.pop(flow_id, None)
            else:
                self.owned[flow_id] = owned
        return _prepare_flow_result(self.hass, owned.kind, result)

    def _abort(self, flow_id: str, owned: OwnedFlow) -> None:
        from homeassistant.data_entry_flow import UnknownFlow

        try:
            self._manager(owned.kind).async_abort(flow_id)
        except UnknownFlow:
            _LOGGER.debug("Owned flow already completed: %s", flow_id)
        self.owned.pop(flow_id, None)

    def _expire(self) -> None:
        now = time.monotonic()
        for flow_id, owned in list(self.owned.items()):
            if not owned.lock.locked() and now - owned.touched >= self.ttl:
                self._abort(flow_id, owned)

    async def stop(self) -> None:
        for flow_id, owned in list(self.owned.items()):
            self._abort(flow_id, owned)
