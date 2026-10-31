"""One self-update signature for MCP and Assist."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import Field


class SelfUpdateFacade:
    def __init__(self, manage: Callable[[str], Awaitable[dict[str, Any]]]) -> None:
        self._manage = manage

    async def ha_manage_ha_mcp_updates(
        self,
        action: Annotated[
            Literal["check", "guide", "update"],
            Field(
                description="Inspect this server's installation and update capability, get its owner-specific guide, or apply a compatible ordinary update through its owner."
            ),
        ] = "check",
    ) -> dict[str, Any]:
        """Update HA-MCP while preserving the current installation and runtime.

        Use ha_manage_updates for other Home Assistant updates. Check first to
        inspect versions, compatibility and restart requirements; guide includes
        all supported installation paths. Breaking or unknown release impacts
        require review before installation through the owner's interface.
        Installed component files become active only after ha_restart and a new
        check; an uncertain write must be inspected rather than replayed.

        EXAMPLES: ha_manage_ha_mcp_updates(action="check"); ha_manage_ha_mcp_updates(action="update").
        """
        return await self._manage(action)
