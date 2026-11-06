"""Owned, bounded Core config-entry subscriptions for reconfigure observation."""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from homeassistant.core import callback
from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.entry_reload import _observe_reload_settled
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

from .owner import CoreOperationOwner

logger = logging.getLogger(__name__)


@dataclass
class EntryWatch:
    actor: tuple[str | None, str]
    entry_id: str
    queue: asyncio.Queue[Any] = field(
        default_factory=lambda: asyncio.Queue(maxsize=128)
    )
    overflow: bool = False
    unsubscribe: Callable[[], None] | None = None
    expiry: asyncio.TimerHandle | None = None


class NativeEntryWatches(CoreOperationOwner):
    def __init__(
        self, *args: Any, limit: int = 32, ttl: float = 150, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.limit, self.ttl = limit, ttl
        self.owned: dict[str, EntryWatch] = {}

    async def execute(
        self, operation: str, arguments: dict[str, Any], actor: CallContext
    ) -> Any:
        if operation == "config.entries.watch":
            await self.authorize(actor)
            return self._open(arguments["entry_id"], actor)
        key = arguments["watch_id"]
        row = self.owned.get(key)
        if row is None or row.actor != (actor.user_id, actor.client_id):
            raise ToolFailure(
                "WATCH_NOT_OWNED",
                "This caller does not own the entry watch.",
                status="not_started",
            )
        if operation == "config.entries.close":
            self._close(key)
            return None
        if (
            operation != "config.entries.observe"
            or arguments["entry_id"] != row.entry_id
        ):
            raise UnsupportedOperation(operation)
        result = await _observe_reload_settled(
            row.queue,
            row.entry_id,
            timeout=min(max(float(arguments.get("timeout", 20)), 0), 30),
        )
        await self.authorize(actor)
        return None if row.overflow else result

    def _open(self, entry_id: str, actor: CallContext) -> dict[str, str]:
        from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED
        from homeassistant.helpers.dispatcher import async_dispatcher_connect

        if len(self.owned) >= self.limit:
            raise ToolFailure(
                "WATCH_LIMIT", "Too many active entry watches.", status="not_started"
            )
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND", "Config entry not found.", status="not_started"
            )
        row = EntryWatch((actor.user_id, actor.client_id), entry_id)
        key = uuid4().hex

        @callback
        def changed(_change: Any, value: Any) -> None:
            if value.entry_id != entry_id:
                return
            if row.queue.full():
                row.overflow = True
                return
            row.queue.put_nowait(
                {"event": [{"entry": json.loads(json_bytes(value.as_json_fragment))}]}
            )

        row.unsubscribe = async_dispatcher_connect(
            self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, changed
        )
        self.owned[key] = row
        try:
            changed(None, entry)
        except BaseException:
            self._close(key)
            raise
        row.expiry = asyncio.get_running_loop().call_later(self.ttl, self._expire, key)
        return {"watch_id": key}

    def _close(self, key: str) -> None:
        row = self.owned.get(key)
        if row is None:
            return
        if row.expiry is not None:
            row.expiry.cancel()
        if row.unsubscribe is not None:
            row.unsubscribe()
        self.owned.pop(key, None)

    def _expire(self, key: str) -> None:
        try:
            self._close(key)
        except Exception:
            logger.exception("Entry watch cleanup failed; retained for stop retry")

    async def stop(self) -> None:
        for key in list(self.owned):
            self._close(key)
