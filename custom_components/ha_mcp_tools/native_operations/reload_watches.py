"""Correlate Core's original post-write reload to its completion event."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from homeassistant.core import callback

from ha_mcp.runtime.config_reload import current_reload_watch
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation


@dataclass
class ReloadWatch:
    actor: tuple[str | None, str]
    domain: str
    timeout: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    hook_task: asyncio.Task[Any] | None = None
    context_id: str | None = None
    confirmed: bool = False
    unsubscribe: list[Callable[[], None]] = field(default_factory=list)
    expiry: asyncio.TimerHandle | None = None


class NativeReloadWatches:
    def __init__(
        self,
        hass: Any,
        authorize: Callable[[CallContext], Awaitable[None]],
        *,
        limit: int = 32,
        ttl: float = 150,
    ) -> None:
        self.hass, self.authorize = hass, authorize
        self.limit, self.ttl = limit, ttl
        self.watches: dict[str, ReloadWatch] = {}

    async def execute(
        self, operation: str, arguments: dict[str, Any], actor: CallContext
    ) -> Any:
        if operation == "config.reload.watch":
            return self._open(arguments, actor)
        watch_id = arguments["watch_id"]
        row = self.watches.get(watch_id)
        if row is None or row.actor != (actor.user_id, actor.client_id):
            raise ToolFailure(
                "WATCH_NOT_OWNED",
                "This caller does not own the reload watch.",
                status="not_started",
            )
        if operation == "config.reload.close":
            self._close(watch_id)
            return None
        if operation != "config.reload.wait":
            raise UnsupportedOperation(operation)
        try:
            await asyncio.wait_for(row.event.wait(), timeout=row.timeout)
        except TimeoutError:
            return False
        await self.authorize(actor)
        return row.confirmed

    def _open(self, arguments: dict[str, Any], actor: CallContext) -> dict[str, str]:
        event_type = arguments["event_type"]
        if event_type not in {"automation_reloaded", "scene_reloaded"}:
            raise UnsupportedOperation(f"config.reload.{event_type}")
        if len(self.watches) >= self.limit:
            raise ToolFailure(
                "WATCH_LIMIT", "Too many active reload watches.", status="not_started"
            )
        watch_id = uuid4().hex
        row = ReloadWatch(
            (actor.user_id, actor.client_id),
            event_type.removesuffix("_reloaded"),
            min(max(float(arguments.get("timeout", 10)), 0), 30),
        )
        self.watches[watch_id] = row

        @callback
        def service_called(event: Any) -> None:
            if (
                row.hook_task is not None
                and asyncio.current_task() is row.hook_task
                and event.data.get("domain") == row.domain
                and event.data.get("service") == "reload"
            ):
                row.context_id = event.context.id

        @callback
        def reloaded(event: Any) -> None:
            if row.context_id is not None and event.context.id == row.context_id:
                row.confirmed = True
                row.event.set()

        try:
            row.unsubscribe.append(
                self.hass.bus.async_listen("call_service", service_called)
            )
            row.unsubscribe.append(self.hass.bus.async_listen(event_type, reloaded))
        except BaseException:
            self._close(watch_id)
            raise
        row.expiry = asyncio.get_running_loop().call_later(
            self.ttl, self._close, watch_id
        )
        return {"watch_id": watch_id}

    async def run_hook(
        self, domain: str, hook: Awaitable[None], actor: CallContext
    ) -> None:
        # The wrapper begins inside Core's HA-owned task, including eager-start
        # tasks. async_run_hass_job invokes @callback bus listeners synchronously,
        # so only this hook can supply the ServiceCall Context being observed.
        row = self.watches.get(current_reload_watch.get() or "")
        if (
            row is None
            or row.domain != domain
            or row.actor != (actor.user_id, actor.client_id)
        ):
            return await hook
        row.hook_task = asyncio.current_task()
        try:
            await hook
        except BaseException:
            row.event.set()
            raise
        finally:
            row.hook_task = None

    def _close(self, watch_id: str) -> None:
        row = self.watches.get(watch_id)
        if row is None:
            return
        if row.expiry is not None:
            row.expiry.cancel()
        while row.unsubscribe:
            row.unsubscribe[0]()
            row.unsubscribe.pop(0)
        self.watches.pop(watch_id, None)
        row.event.set()

    async def stop(self) -> None:
        for watch_id in list(self.watches):
            self._close(watch_id)
