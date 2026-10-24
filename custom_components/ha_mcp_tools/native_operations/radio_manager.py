"""Own each radio call while its concrete Core integration carries out the work."""

import asyncio
import copy
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.runtime.radio_values import integration_not_found
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .radio_access import RadioSession, unavailable
from .registry_access import Authorize, authorize_actor

_DOMAINS = {"zwave": "zwave_js", "zigbee": "zha", "matter": "matter", "thread": "otbr"}


@dataclass
class NativeRadios:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(repr=False)
    session: RadioSession = field(init=False)

    def __post_init__(self) -> None:
        self.session = RadioSession(self.hass, self.actor, self.authorize)

    async def resolve_entity_device(self, entity_id: str) -> str:
        await authorize_actor(self.actor, self.authorize)
        registry = self.session.registry("entity")
        entity = registry.async_get(entity_id)
        if entity is None or not entity.device_id:
            raise ToolFailure(
                "ENTITY_NOT_FOUND",
                "Entity has no associated radio device",
                status="not_started",
            )
        self.session.retain(lambda: registry.async_get(entity_id), entity)
        return str(entity.device_id)

    async def handle(
        self, radio: str, action: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        await authorize_actor(self.actor, self.authorize)
        # Own the entire preparation/effect sequence, including repeated client
        # cancellation; no hardware operation is replayed on another transport.
        async with registry_update_lock("radio", radio):
            return await await_owned_io(
                self._handle(radio, action, copy.deepcopy(args))
            )

    async def _handle(
        self, radio: str, action: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        domain = _DOMAINS[radio]
        if (
            action == "network_status"
            and not self.hass.config_entries.async_loaded_entries(domain)
        ):
            return integration_not_found(radio, domain)
        try:
            if radio == "matter":
                from .radio_matter import handle_matter

                return await handle_matter(self.session, action, args)
            if radio == "zigbee":
                from .radio_zigbee import handle_zigbee

                return await handle_zigbee(self.session, action, args)
            if radio == "zwave":
                from .radio_zwave import handle_zwave

                return await handle_zwave(self.session, action, args)
            if (
                action
                not in {
                    "network_status",
                    "create_network",
                    "set_network",
                    "set_channel",
                }
                and "thread" not in self.hass.config.components
            ):
                raise unavailable("thread")
            from .radio_thread import handle_thread

            return await handle_thread(self.session, action, args)
        except (Exception, asyncio.CancelledError) as error:
            if not self.session.dispatched:
                raise
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "The Core radio operation was dispatched and may have acted. Inspect the integration before issuing another command; no retry was performed.",
                status="outcome_unknown",
            ) from error
