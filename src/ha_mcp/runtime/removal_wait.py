"""An acknowledged deletion stays successful when later verification fails."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp.errors import HomeAssistantAuthError, HomeAssistantConnectionError

logger = logging.getLogger(__name__)


async def verify_removed(
    wait_for_entity: Callable[[str], Awaitable[bool]],
    entity_id: str,
    response: dict[str, Any],
) -> None:
    """After a delete that succeeded, warn when ``entity_id`` lingers or its removal
    cannot be checked; the delete itself is never reported as failed."""
    try:
        removed = await wait_for_entity(entity_id)
    except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
        response.setdefault("warnings", []).append(
            f"Deletion confirmed but removal verification failed: {e}"
        )
        return
    except Exception as e:  # cancellation is a BaseException and propagates
        logger.warning(
            f"Unexpected error verifying removal of {entity_id}: {e}", exc_info=True
        )
        response.setdefault("warnings", []).append(
            f"Deletion confirmed but removal verification failed: {e}"
        )
        return
    if not removed:
        response.setdefault("warnings", []).append(
            f"Deletion confirmed but {entity_id} is still present after the "
            "wait window."
        )
