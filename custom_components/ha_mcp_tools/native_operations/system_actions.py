"""Restart/reload through the actual Core owners, with no self-stop wait cycle."""

import asyncio
from dataclasses import dataclass
from typing import Any

from homeassistant.core import Context

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.runtime.system_values import (
    RELOAD_TARGETS,
    reload_all_result,
    reload_entry_result,
    reload_target_result,
    restart_precheck_warnings,
    restart_scheduled_result,
)
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from .registry_access import admit_registry_write, authorize_actor


@dataclass(slots=True)
class NativeSystemActions:
    manager: Any
    actor: CallContext

    async def restart(self, safe_mode: bool = False) -> dict[str, Any]:
        from homeassistant import config

        await admit_registry_write(self.actor, self.manager.backend.authorize)
        errors = await await_owned_io(
            config.async_check_ha_config_file(self.manager.hass)
        )
        await admit_registry_write(self.actor, self.manager.backend.authorize)
        if errors:
            raise_failure(
                create_error_response(
                    ErrorCode.CONFIG_INVALID,
                    "Configuration is invalid - restart aborted",
                    details="Home Assistant configuration has errors. Fix the errors before restarting.",
                    context={"config_errors": errors},
                )
            )
        self._require_service("homeassistant", "restart")
        warnings = self.restart_precheck(safe_mode=safe_mode)
        self.manager.system_lifecycle.schedule(
            "restart", self.actor, safe_mode=safe_mode
        )
        result = restart_scheduled_result()
        result["warnings"].extend(warnings)
        return result

    def restart_precheck(self, *, safe_mode: bool = False) -> list[str]:
        from homeassistant.components.backup import async_get_manager
        from homeassistant.exceptions import HomeAssistantError
        from homeassistant.helpers.recorder import async_migration_in_progress

        hass = self.manager.hass
        migration = async_migration_in_progress(hass)
        try:
            manager = async_get_manager(hass)
        except HomeAssistantError:
            backup_info = None
        else:
            backup_info = {"state": getattr(manager, "state", None)}
        return restart_precheck_warnings(
            {"migration_in_progress": migration},
            backup_info,
            safe_mode=safe_mode,
        )

    def _require_service(self, domain: str, service: str) -> None:
        if not self.manager.hass.services.has_service(domain, service):
            raise ToolFailure(
                "SERVICE_NOT_FOUND",
                f"Service not found: {domain}.{service}",
                status="not_started",
            )

    async def reload(self, target: str, entry_id: str | None) -> dict[str, Any]:
        await admit_registry_write(self.actor, self.manager.backend.authorize)
        if entry_id is not None:
            return await self._entry(entry_id)
        if target == "all":
            return await self._all()
        service = RELOAD_TARGETS[target]
        assert service is not None
        await await_owned_io(self._service(target, *service))
        return reload_target_result(target, *service)

    async def _service(self, target: str, domain: str, service: str) -> None:
        await admit_registry_write(self.actor, self.manager.backend.authorize)
        self._require_service(domain, service)
        try:
            await self.manager.hass.services.async_call(
                domain,
                service,
                {},
                blocking=True,
                context=Context(user_id=self.actor.user_id),
            )
            await authorize_actor(self.actor, self.manager.backend.authorize)
        except (Exception, asyncio.CancelledError) as error:
            raise self._unknown(target, error) from error

    async def _all(self) -> dict[str, Any]:
        targets = [
            (name, service)
            for name, service in RELOAD_TARGETS.items()
            if service is not None and self.manager.hass.services.has_service(*service)
        ]
        semaphore = asyncio.Semaphore(4)

        async def reload_target(name: str, service: tuple[str, str]) -> None:
            async with semaphore:
                await self._service(name, *service)

        outcomes = await await_owned_io(
            asyncio.gather(
                *(reload_target(name, service) for name, service in targets),
                return_exceptions=True,
            )
        )
        reloaded = []
        errors = []
        unknown = False
        for (name, _service), outcome in zip(targets, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, ToolFailure):
                    raise outcome
                unknown |= outcome.status == "outcome_unknown"
                errors.append(f"{name}: {outcome}")
            else:
                reloaded.append(name)
        try:
            await authorize_actor(self.actor, self.manager.backend.authorize)
        except Exception as error:
            if reloaded or unknown:
                raise self._unknown("all", error) from error
            raise
        result = reload_all_result(reloaded, errors)
        if errors:
            result["partial"] = True
        if unknown:
            result["operation_outcome"] = "outcome_unknown"
        return result

    async def _entry(self, entry_id: str) -> dict[str, Any]:
        entries = self.manager.hass.config_entries
        entry = entries.async_get_entry(entry_id)
        if entry is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND",
                f"Config entry not found: {entry_id}",
                status="not_started",
            )
        if not entry.state.recoverable:
            failure = ToolFailure(
                "SERVICE_CALL_FAILED",
                f"Config entry {entry_id} cannot be reloaded in its current state; a Home Assistant restart is required.",
                status="not_started",
            )
            failure.data.update(entry_id=entry_id, reloaded=False, require_restart=True)
            raise failure
        await admit_registry_write(self.actor, self.manager.backend.authorize)
        if (
            entries is not self.manager.hass.config_entries
            or entries.async_get_entry(entry_id) is not entry
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Config entry changed before reload.",
                status="not_started",
            )
        if entry is self.manager.entry:
            self.manager.system_lifecycle.schedule(
                "entry_reload", self.actor, entry=entry
            )
            return {
                "success": True,
                "requested": True,
                "scheduled": True,
                "reloaded": False,
                "require_restart": False,
                "entry_id": entry_id,
                "message": "This MCP server's config-entry reload is scheduled. Its completion cannot be confirmed by this invocation.",
            }
        return await await_owned_io(self._reload_entry(entries, entry))

    async def _reload_entry(self, entries: Any, entry: Any) -> dict[str, Any]:
        await admit_registry_write(self.actor, self.manager.backend.authorize)
        if (
            entries is not self.manager.hass.config_entries
            or entries.async_get_entry(entry.entry_id) is not entry
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Config entry changed before reload.",
                status="not_started",
            )
        try:
            loaded = await entries.async_reload(entry.entry_id)
            await authorize_actor(self.actor, self.manager.backend.authorize)
            if (
                entries is not self.manager.hass.config_entries
                or entries.async_get_entry(entry.entry_id) is not entry
            ):
                raise RuntimeError("Config entry identity changed during reload")
            require_restart = not entry.state.recoverable
            if type(loaded) is not bool:
                raise RuntimeError(
                    "Core returned an unknown config-entry reload outcome"
                )
        except (Exception, asyncio.CancelledError) as error:
            raise self._unknown(entry.entry_id, error) from error
        if not loaded and not require_restart:
            failure = ToolFailure(
                "SERVICE_CALL_FAILED",
                "Core completed the reload attempt but did not load this config entry. Inspect ha_get_integration before deciding on another reload.",
                status="confirmed",
            )
            failure.data.update(
                entry_id=entry.entry_id, reloaded=False, require_restart=False
            )
            raise failure
        return reload_entry_result(entry.entry_id, require_restart)

    @staticmethod
    def _unknown(target: str, error: BaseException) -> ToolFailure:
        failure = ToolFailure(
            "OUTCOME_UNKNOWN",
            f"Core reload of {target} started but did not complete normally ({type(error).__name__}). Inspect the current integration state before deciding on another action; no reload was repeated.",
            status="outcome_unknown",
        )
        failure.data["target"] = target
        return failure
