"""Read-only cached store details, shared with the remote component bridge."""

from typing import Any

from ha_mcp.tool_api import CallContext

from .hacs_access import HacsAccess, repository_image
from .marketplace_access import MarketplaceAccess, store_domain
from .registry_access import registry_json


async def cached_store_info(
    hass: Any, user_id: str, repository_id: str
) -> dict[str, Any]:
    """Read the loaded store for an actual upstream WS caller, with no runtime."""

    async def authorize(actor: CallContext) -> None:
        user = await hass.auth.async_get_user(actor.user_id)
        if (
            user is None
            or not user.is_active
            or not user.is_admin
            or user.system_generated
        ):
            raise PermissionError("An active human administrator is required")

    access_type = (
        MarketplaceAccess if store_domain(hass) == "marketplace" else HacsAccess
    )
    access = await access_type.open(hass, CallContext(user_id), authorize)
    repository = access.find(repository_id)
    before = repository_image(repository)
    result = {
        "store": access.domain,
        "repository": access.project(repository),
        "cache": access.cache(repository),
    }
    await access.observe(repository, before)
    return registry_json(result)
