"""Integration state changes delegate to Core's logger and config-entry owners."""

import json
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.flows.entry import refuse_component_entry
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .owner import CoreOperationOwner
from .registry_mutations import _validated_payload

INTEGRATION_STATE_OPERATIONS = frozenset(
    {"integrations.log_level.set", "integrations.enabled.set"}
)


class NativeIntegrationState(CoreOperationOwner):
    async def execute(
        self, operation: str, arguments: dict[str, Any], actor: CallContext
    ) -> dict[str, Any]:
        if operation == "integrations.log_level.set":
            return await await_owned_io(self._log_level(arguments, actor))
        if operation == "integrations.enabled.set":
            return await await_owned_io(self._enabled(arguments, actor))
        raise UnsupportedOperation(operation)

    async def _admit(self, actor: CallContext) -> None:
        try:
            await self.authorize(actor)
            check_mutation_admission(actor)
        except PermissionError as error:
            raise ToolFailure(
                "AUTH_REQUIRED",
                "Integration admission was revoked.",
                status="not_started",
            ) from error
        if actor.read_only:
            raise ToolFailure("READ_ONLY", "Writes are disabled.", status="not_started")

    async def _log_level(
        self, arguments: dict[str, Any], actor: CallContext
    ) -> dict[str, Any]:
        from homeassistant.components.logger.helpers import (
            DATA_LOGGER,
            LoggerSetting,
            LogSettingsType,
        )
        from homeassistant.loader import IntegrationNotFound, async_get_integration

        data = _validated_payload(
            self.hass,
            "logger/integration_log_level",
            {
                "integration": arguments["domain"],
                "level": arguments["level"],
                "persistence": "once",
            },
        )
        domain = data["integration"]
        refuse_component_entry(domain, action="set the log level of")
        try:
            await async_get_integration(self.hass, domain)
        except IntegrationNotFound:
            return {
                "success": False,
                "error_code": "not_found",
                "error": "Integration not found",
            }
        owner = self.hass.data.get(DATA_LOGGER)
        if owner is None:
            raise UnsupportedOperation("integrations.log_level.owner")
        async with registry_update_lock("integration_logger", domain):
            await self._admit(actor)
            try:
                await owner.settings.async_update(
                    self.hass,
                    domain,
                    LoggerSetting(
                        level=data["level"],
                        persistence=data["persistence"],
                        type=LogSettingsType.INTEGRATION,
                    ),
                )
            except Exception as error:
                raise ToolFailure(
                    "OUTCOME_UNKNOWN",
                    "Core logger update was dispatched but did not complete cleanly.",
                    status="outcome_unknown",
                ) from error
        return {"success": True}

    async def _enabled(
        self, arguments: dict[str, Any], actor: CallContext
    ) -> dict[str, Any]:
        from homeassistant.config_entries import (
            ConfigEntryDisabler,
            OperationNotAllowed,
            UnknownEntry,
        )

        data = _validated_payload(
            self.hass,
            "config_entries/disable",
            {
                "entry_id": arguments["entry_id"],
                "disabled_by": None if arguments["enabled"] else "user",
            },
        )
        entry_id = data["entry_id"]
        async with registry_update_lock("config_entry", entry_id):
            await self._admit(actor)
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is None:
                return {
                    "success": False,
                    "error_code": "not_found",
                    "error": "Config entry not found",
                }
            refuse_component_entry(
                entry.domain, action="enable or disable", entry_id=entry_id
            )
            if "expected_entry" in arguments and not json_values_equal(
                json.loads(json_bytes(entry.as_json_fragment)),
                arguments["expected_entry"],
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The integration changed before its state update.",
                    status="not_started",
                )
            disabled_by = (
                ConfigEntryDisabler(data["disabled_by"])
                if data["disabled_by"] is not None
                else None
            )
            try:
                success = await self.hass.config_entries.async_set_disabled_by(
                    entry_id, disabled_by
                )
            except OperationNotAllowed:
                success = False
            except UnknownEntry:
                return {
                    "success": False,
                    "error_code": "not_found",
                    "error": "Config entry not found",
                }
            except Exception as error:
                raise ToolFailure(
                    "OUTCOME_UNKNOWN",
                    "Core integration state update was dispatched but did not complete cleanly.",
                    status="outcome_unknown",
                ) from error
        return {"success": True, "result": {"require_restart": not success}}
