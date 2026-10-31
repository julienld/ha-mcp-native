"""Bounded, best-effort progress independent of any MCP or Assist context."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)
PROGRESS_TIMEOUT = 1.0


async def report_progress(
    report: Callable[..., Awaitable[Any]] | None,
    *,
    progress: float,
    total: float | None = None,
    message: str | None = None,
) -> None:
    if report is None:
        return
    try:
        async with asyncio.timeout(PROGRESS_TIMEOUT):
            await report(progress=progress, total=total, message=message)
    except (TypeError, AttributeError) as error:
        logger.warning(
            "ctx.report_progress signature error (%s): %s", type(error).__name__, error
        )
    except Exception as error:  # noqa: BLE001 - optional notifications never invalidate a tool result
        logger.debug("ctx.report_progress failed (%s): %s", type(error).__name__, error)
