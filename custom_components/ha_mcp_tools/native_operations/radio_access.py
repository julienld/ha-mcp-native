"""Per-call radio admission, frozen owners and owned effect dispatch."""

import asyncio
import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant, callback

from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import json_values_equal

from .command_validation import _validated_payload
from .registry_access import (
    Authorize,
    admit_registry_write,
    authorize_actor,
    registry_json,
)


def unavailable(domain: str) -> ToolFailure:
    return ToolFailure(
        "RADIO_UNAVAILABLE",
        f"The {domain} integration or its radio owner is not loaded",
        status="not_started",
    )


@dataclass
class RadioSession:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize
    pins: list[Callable[[], bool]] = field(default_factory=list)
    target_pins: list[Callable[[], bool]] = field(default_factory=list)
    dispatched: bool = False

    def retain(
        self, getter: Callable[[], Any], expected: Any, *, target: bool = False
    ) -> Any:
        (self.target_pins if target else self.pins).append(lambda: getter() is expected)
        return expected

    def freeze(self, getter: Callable[[], Any]) -> None:
        before = copy.deepcopy(getter())
        self.target_pins.append(lambda: json_values_equal(getter(), before))

    def verify(self, *, owners_only: bool = False) -> None:
        if not all(
            pin() for pin in [*self.pins, *([] if owners_only else self.target_pins)]
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Radio owner or target changed during preparation",
                status="not_started",
            )

    def entry(self, domain: str, entry_id: str | None = None) -> Any:
        entries = self.hass.config_entries.async_loaded_entries(domain)
        entry = next(
            (
                entry
                for entry in entries
                if entry_id is None or entry.entry_id == entry_id
            ),
            None,
        )
        if entry is None:
            raise unavailable(domain)
        self.retain(
            lambda: self.hass.config_entries.async_get_entry(entry.entry_id), entry
        )
        self.pins.append(lambda: entry.state is ConfigEntryState.LOADED)
        self.freeze(lambda: {"data": dict(entry.data), "options": dict(entry.options)})
        return entry

    def runtime_owner(self, entry: Any, attribute: str) -> Any:
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None or isinstance(runtime, asyncio.Future):
            raise unavailable(entry.domain + " runtime")
        self.retain(lambda: entry.runtime_data, runtime)
        owner = getattr(runtime, attribute, None)
        if owner is None or isinstance(owner, asyncio.Future):
            raise unavailable(entry.domain + " " + attribute)
        return self.retain(lambda: getattr(runtime, attribute), owner)

    def validate(self, command: str, values: dict[str, Any]) -> dict[str, Any]:
        registration = self.hass.data.get("websocket_api", {}).get(command)
        self.retain(
            lambda: self.hass.data.get("websocket_api", {}).get(command), registration
        )
        return _validated_payload(
            self.hass,
            command,
            {key: value for key, value in values.items() if value is not None},
        )

    async def read(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        await authorize_actor(self.actor, self.authorize)
        self.verify()
        result = await operation()
        await authorize_actor(self.actor, self.authorize)
        self.verify()
        return result

    async def effect(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        # This method is called only inside the call-owned worker. The final
        # admission follows preparation and directly precedes the Core action.
        await admit_registry_write(self.actor, self.authorize)
        self.verify()
        self.dispatched = True
        result = await operation()
        await authorize_actor(self.actor, self.authorize)
        self.verify(owners_only=True)
        return result

    async def sync_effect(self, operation: Callable[[], Any]) -> Any:
        await admit_registry_write(self.actor, self.authorize)
        self.verify()
        self.dispatched = True
        return operation()

    def registry(self, kind: str) -> Any:
        owner = self.hass.data.get(kind + "_registry")
        if owner is None or isinstance(owner, asyncio.Future):
            raise unavailable(kind + " registry")
        return self.retain(lambda: self.hass.data.get(kind + "_registry"), owner)

    def device(self, identifier: str) -> Any:
        registry = self.registry("device")
        row = registry.async_get(identifier)
        if row is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND",
                "Radio device is not in the device registry",
                status="not_started",
            )
        self.retain(lambda: registry.async_get(identifier), row, target=True)
        self.freeze(
            lambda: {
                "identifiers": sorted(row.identifiers),
                "config_entries": sorted(row.config_entries),
            }
        )
        return row

    async def service(
        self, domain: str, service: str, data: dict[str, Any], *, writes: bool = True
    ) -> Any:
        if not self.hass.services.has_service(domain, service):
            raise unavailable(domain + "." + service)
        registered = self.hass.services.async_services()[domain][service]
        self.retain(
            lambda: self.hass.services.async_services().get(domain, {}).get(service),
            registered,
        )
        payload = (
            registered.schema(copy.deepcopy(data))
            if registered.schema
            else copy.deepcopy(data)
        )

        async def call() -> Any:
            context = Context(user_id=self.actor.user_id)
            changed = []

            @callback
            def observed(event: Any) -> None:
                if (
                    event.context == context
                    and (state := event.data.get("new_state")) is not None
                ):
                    changed.append(state.as_dict())

            unsubscribe = self.hass.bus.async_listen("state_changed", observed)
            try:
                await self.hass.services.async_call(
                    domain,
                    service,
                    payload,
                    blocking=True,
                    context=context,
                )
            finally:
                unsubscribe()
            return registry_json({"states": changed})["states"]

        return await (self.effect(call) if writes else self.read(call))

    async def firmware(self, device_id: str, platform: str) -> str:
        self.device(device_id)
        registry = self.registry("entity")
        candidates = [
            row
            for row in registry.entities.values()
            if row.device_id == device_id and row.entity_id.startswith("update.")
        ]
        if not candidates:
            raise ToolFailure(
                "ENTITY_NOT_FOUND",
                "Device has no firmware update entity",
                status="not_started",
            )
        entity = next(
            (row for row in candidates if row.platform == platform), candidates[0]
        )
        self.retain(lambda: registry.async_get(entity.entity_id), entity, target=True)
        await self.service("update", "install", {"entity_id": entity.entity_id})
        return str(entity.entity_id)
