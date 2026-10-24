"""Existing remote config-entry change subscriptions and their bounded ownership."""

import logging
from typing import Any
from uuid import uuid4

from ha_mcp.client.websocket_client import get_websocket_client
from ha_mcp.runtime.entry_reload import (
    _ENTRY_SUBSCRIBE_TIMEOUT,
    WS_CONFIG_ENTRIES_SUBSCRIBE,
    _observe_reload_settled,
)
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io

logger = logging.getLogger(__name__)


class RemoteEntryWatches:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.owned: dict[str, tuple[Any, tuple[str | None, str]]] = {}
        self.starting = 0

    async def execute(
        self, operation: str, arguments: dict[str, Any], actor: CallContext
    ) -> Any:
        if operation == "config.entries.watch":
            if len(self.owned) + self.starting >= 32:
                raise ToolFailure(
                    "WATCH_LIMIT",
                    "Too many active entry watches.",
                    status="not_started",
                )
            self.starting += 1
            try:
                subscription = await _subscribe_entry_changes(self.client)
                if subscription is None:
                    return None
                key = uuid4().hex
                self.owned[key] = subscription, (actor.user_id, actor.client_id)
                return {"watch_id": key}
            finally:
                self.starting -= 1
        key = arguments["watch_id"]
        record = self.owned.get(key)
        if record is None or record[1] != (actor.user_id, actor.client_id):
            raise ToolFailure(
                "WATCH_NOT_OWNED",
                "This caller does not own the entry watch.",
                status="not_started",
            )
        subscription, _ = record
        if operation == "config.entries.close":
            await self._close(key)
            return None
        if operation != "config.entries.observe":
            raise UnsupportedOperation(operation)
        _, (_, queue) = subscription
        return await _observe_reload_settled(
            queue,
            arguments["entry_id"],
            timeout=min(max(float(arguments.get("timeout", 20)), 0), 30),
        )

    async def _close(self, key: str) -> None:
        ws, (subscription_id, _) = self.owned[key][0]
        await await_owned_io(ws.unsubscribe_command(subscription_id))
        self.owned.pop(key, None)

    async def stop(self) -> None:
        for key in list(self.owned):
            await self._close(key)


async def _subscribe_entry_changes(client: Any) -> tuple[Any, Any] | None:
    """Open a config-entry change stream, or ``None`` if unavailable.

    Home Assistant dispatches ``SIGNAL_CONFIG_ENTRY_CHANGED`` from
    ``ConfigEntry._async_set_state``, so ``config_entries/subscribe`` pushes
    EVERY state transition with the full entry fragment. Subscribing before
    the flow starts is what makes the post-commit reload observable instead of
    sampled: the queue is registered before the frame is sent, so no
    transition between the commit and our first read can be missed.
    """
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        sub_id, queue = await ws.subscribe_command(
            WS_CONFIG_ENTRIES_SUBSCRIBE, wait_timeout=_ENTRY_SUBSCRIBE_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001
        # Degrade to polling rather than failing the reconfigure; the caller
        # records which mechanism actually ran.
        logger.warning(
            "%s unavailable (%r); falling back to polled verification",
            WS_CONFIG_ENTRIES_SUBSCRIBE,
            exc,
        )
        return None
    return ws, (sub_id, queue)
