"""Observe reload completion around a write using a bounded backend scope."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar

from ha_mcp.tool_api import Backend, CallContext
from ha_mcp.utils.owned_executor import await_owned_io

current_reload_watch: ContextVar[str | None] = ContextVar(
    "ha_mcp_reload_watch", default=None
)
_LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def config_reload_waiter(
    backend: Backend,
    context: CallContext,
    event_type: str,
    *,
    enabled: bool = True,
    timeout: float = 10.0,
) -> AsyncIterator[Callable[[], Awaitable[bool | None]]]:
    if not enabled:

        async def not_watched() -> None:
            return None

        yield not_watched
        return
    opened = await backend.execute(
        "config.reload.watch", {"event_type": event_type, "timeout": timeout}, context
    )
    watch_id = opened["watch_id"]
    token = current_reload_watch.set(watch_id)

    async def wait_for_reload() -> bool | None:
        verdict: bool | None = await backend.execute(
            "config.reload.wait", {"watch_id": watch_id}, context
        )
        return verdict

    try:
        yield wait_for_reload
    finally:
        current_reload_watch.reset(token)
        try:
            await await_owned_io(
                backend.execute("config.reload.close", {"watch_id": watch_id}, context)
            )
        except Exception as error:  # noqa: BLE001 - cleanup cannot replace the write outcome
            # The native owner also expires scopes and removes them at stop.
            _LOGGER.warning(
                "Reload observation cleanup failed (%s)", type(error).__name__
            )
