"""The store's normal info refresh, without dispatching its WebSocket handler."""

import asyncio
import logging
from importlib import import_module
from typing import Any

from ha_mcp.tool_api import ToolFailure

from .hacs_access import HacsAccess

logger = logging.getLogger(__name__)


async def refresh_info(
    access: HacsAccess, repository: Any, registration: Any
) -> tuple[bool, list[str]]:
    refreshed, warnings = False, []
    await access.observe(repository)
    access.pin_validator(access.command("info"), registration)
    if access.domain == "hacs" and access.owner.system.disabled:
        return False, ["HACS is disabled; cached repository details are returned."]
    if not repository.updated_info:
        try:
            await repository.update_repository(ignore_issues=True, force=True)
        except asyncio.CancelledError as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Store info refresh was interrupted after dispatch; metadata may have changed. It was not replayed.",
                status="outcome_unknown",
            ) from error
        except Exception as error:
            logger.debug("Store info refresh failed", exc_info=True)
            warnings.append(
                "Store metadata could not be refreshed; cached details are returned."
            )
            await access.observe(repository)
            await _refresh_failed(access, repository, error)

        else:
            await access.observe(repository)
            repository.updated_info = True
            refreshed = True
    await access.observe(repository)
    access.pin_validator(access.command("info"), registration)
    if repository.data.new:
        repository.data.new = False
        try:
            await access.storage.async_write()
        except (Exception, asyncio.CancelledError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "The store info metadata changed but saving could not be confirmed. It was not replayed.",
                status="outcome_unknown",
            ) from error
    await access.observe(repository)
    access.pin_validator(access.command("info"), registration)
    return refreshed, warnings


async def _refresh_failed(
    access: HacsAccess, repository: Any, error: Exception
) -> None:
    if access.domain != "marketplace":
        repository.updated_info = True
        return
    errors = import_module("homeassistant.components.marketplace.exceptions")
    if not isinstance(error, errors.MarketplaceError):
        raise error
    if isinstance(error, errors.GitHubAnonymousRateLimitError):
        if not repository.additional_info:
            repository.additional_info = await repository.async_get_readme_contents()
    else:
        repository.updated_info = True
