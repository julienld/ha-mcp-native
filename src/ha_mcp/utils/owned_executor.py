"""Keep an executor operation's resource owner alive through cancellation."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


async def await_owned_io[T](operation: Awaitable[T]) -> T:
    """Finish issued I/O before cancellation releases its caller's locks/pins."""
    worker = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Cancelling an await cannot stop an executor thread. Repeated cancels
        # must not release the lock while that thread can still mutate data.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - observe the failure, preserve cancellation
                break
        if not worker.cancelled():
            worker.exception()
        raise


async def await_backup_io[T](operation: Callable[..., T], *args: Any) -> T:
    """Compatibility entry for backup I/O that owns its own executor dispatch."""
    return await await_owned_io(asyncio.to_thread(operation, *args))
