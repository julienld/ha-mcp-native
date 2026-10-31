"""Self-update through Core's UpdateEntity owner; keep loaded code until restart."""

import asyncio
import copy
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from ha_mcp import __version__
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.self_update_guides import installation_guides, update_guide
from ha_mcp.self_update_releases import assess_releases
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .const import COMPONENT_VERSION, OPT_RUNTIME_MODE, RUNTIME_LEGACY
from .native_operations.hacs_access import check_self_files
from .native_operations.hacs_updates import (
    call_install_service,
    install_service,
    pin_install_service,
)
from .native_operations.registry_access import admit_registry_write, authorize_actor
from .native_operations.updates import NativeUpdates
from .native_self_update_owner import SelfUpdateOwner, find_self_update
from .native_self_update_storage import (
    capture_code,
    component_source,
    read_receipt,
    recovery_summary,
    write_receipt,
)


class NativeSelfUpdate:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.running_component_version = COMPONENT_VERSION
        self.running_server_version = __version__
        self.directory = Path(
            manager.hass.config.path(".storage", "ha_mcp_self_update")
        )

    async def _io(self, function: Any, *arguments: Any) -> Any:
        return await await_owned_io(
            self.manager.hass.async_add_executor_job(function, *arguments)
        )

    async def manage(self, action: str, actor: CallContext) -> dict[str, Any]:
        await authorize_actor(actor, self.manager.backend.authorize)
        updates = NativeUpdates(self.manager, actor)
        discovery_error = None
        try:
            owner = await find_self_update(self.manager, actor, await updates._hidden())
        except ToolFailure as error:
            if action == "update" or error.code not in (
                "COMPONENT_NOT_INSTALLED",
                "SERVICE_UNAVAILABLE",
                "CONFIG_CONFLICT",
            ):
                raise
            owner, discovery_error = None, error.code
        receipt = await self._io(read_receipt, self.directory)
        await authorize_actor(actor, self.manager.backend.authorize)
        if owner is not None:
            owner.check(self.manager.hass)
        result = self._describe(owner)
        if discovery_error is not None:
            result["owner_discovery_error"] = discovery_error
        if receipt is not None:
            result["recovery"] = recovery_summary(receipt)
        if action != "update":
            if action == "guide":
                result["installation_methods"] = installation_guides()
            if self._unresolved(receipt, owner):
                result["status"] = "outcome_unknown"
            return result
        return await self._update(owner, actor, result, receipt)

    async def _update(
        self,
        owner: SelfUpdateOwner | None,
        actor: CallContext,
        result: dict[str, Any],
        receipt: Any,
    ) -> dict[str, Any]:
        await self._admit_update(actor)
        if self._unresolved(receipt, owner):
            self._fail(
                "OUTCOME_UNKNOWN",
                "A previous dispatch is unresolved. Inspect the owner and recovery image; this tool will not replay it.",
                result,
                "outcome_unknown",
            )
        if owner is None or result["capability"] != "install":
            self._fail(
                "UNSUPPORTED_OPERATION",
                "Use the returned installation-owner guide.",
                result,
            )
        if result["status"] == "pending_restart":
            return result
        if result["impact"]["classification"] == "unchanged":
            return result
        if result["impact"]["classification"] != "compatible":
            self._fail(
                "VALIDATION_INVALID_PARAMETER",
                "Review the breaking or unknown release impact before installing through the owner's interface.",
                result,
            )
        assert owner is not None
        async with (
            registry_update_lock("hacs", "repositories"),
            registry_update_lock("update", owner.target.entity_id),
        ):
            receipt = await self._io(read_receipt, self.directory)
            if self._unresolved(receipt, owner):
                self._fail(
                    "OUTCOME_UNKNOWN",
                    "An earlier dispatch is unresolved; it will not be replayed.",
                    result,
                    "outcome_unknown",
                )
            return await await_owned_io(self._install(owner, actor, result))

    async def _admit_update(self, actor: CallContext) -> None:
        visibility = getattr(self.manager, "visibility", None)
        if visibility is not None:
            await visibility.before(
                "ha_manage_ha_mcp_updates", {"action": "update"}, actor
            )
        await admit_registry_write(actor, self.manager.backend.authorize)

    @staticmethod
    def _unresolved(receipt: Any, owner: SelfUpdateOwner | None) -> bool:
        if not receipt or receipt["state"] != "dispatching":
            return False
        return owner is None or not (
            receipt["owner"] == owner.provider
            and receipt.get("repository") == owner.repository_name
            and receipt.get("repository_id") == str(owner.repository.data.id)
            and receipt["entity_id"] == owner.target.entity_id
            and receipt["target_version"] == owner.target.entity.installed_version
            and not owner.target.entity.in_progress
        )

    def _describe(self, owner: SelfUpdateOwner | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "success": True,
            "installation": {
                "method": "component",
                "runtime": self.manager.entry.options.get(
                    OPT_RUNTIME_MODE, RUNTIME_LEGACY
                ),
            },
            "running_version": self.running_component_version,
            "running_server_version": self.running_server_version,
            "installed_version": None,
            "available_version": None,
            "capability": "manual_required",
            "status": "not_started",
            "guide": update_guide("component"),
            "restart": {
                "required_after_install": True,
                "automatic": False,
                "kind": "expected_interruption",
                "next_tool": "ha_restart",
            },
        }
        if owner is None:
            result["limitations"] = [
                "No unambiguous visible update entity owned by the installed HA-MCP repository is available."
            ]
            return result
        entity, repo = owner.target.entity, owner.repository
        result.update(
            owner=owner.provider,
            repository=owner.repository_name,
            provenance="verified_current_origin"
            if owner.provenance_verified
            else "unverified_fork_pairing",
            owner_consent={
                "required": owner.provider == "marketplace",
                "authority": "Existing store consent, enforced by Core update.install for the current actor",
            },
            entity_id=owner.target.entity_id,
            installed_version=entity.installed_version,
            available_version=entity.latest_version,
            impact=assess_releases(
                str(entity.installed_version),
                str(entity.latest_version),
                repo.releases.objects,
            ),
            status="pending_restart" if self._pending_restart(owner) else "checked",
        )
        if (
            (owner.provider == "marketplace" or str(owner.owner.version) == "2.0.5")
            and entity.available
            and owner.provenance_verified
        ):
            result["capability"] = "install"
        else:
            result["limitations"] = [
                "The installation origin or owner's self-replacement lifecycle could not be verified; use its current repository's update interface. This tool never substitutes upstream for a fork."
            ]
        return result

    def _pending_restart(self, owner: SelfUpdateOwner) -> bool:
        if owner.repository.pending_restart:
            return True
        try:
            installed = Version(str(owner.target.entity.installed_version))
            running = (
                self.running_server_version
                if installed.is_prerelease
                else self.running_component_version
            )
            return installed != Version(running)
        except InvalidVersion:
            return False

    def _pin(
        self, owner: SelfUpdateOwner, before: dict[str, Any], entry_image: Any
    ) -> None:
        owner.check(self.manager.hass)
        current = self._describe(owner)
        if any(
            current[key] != before[key]
            for key in (
                "installed_version",
                "available_version",
                "installation",
                "impact",
            )
        ) or entry_image != (
            dict(self.manager.entry.data),
            dict(self.manager.entry.options),
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The release or server configuration changed during preparation.",
                status="not_started",
            )
        owner.access.pin(owner.repository, writes=True)
        owner.access.guard("download", owner.repository)
        if owner.provider == "hacs":
            check_self_files(owner.owner, owner.repository, "ha_mcp_tools")

    async def _install(
        self, owner: SelfUpdateOwner, actor: CallContext, before: dict[str, Any]
    ) -> dict[str, Any]:
        from homeassistant.components.update import UpdateEntityFeature

        if (
            UpdateEntityFeature.INSTALL not in owner.target.entity.supported_features
            or owner.target.entity.in_progress
        ):
            self._fail(
                "SERVICE_UNAVAILABLE", "The owner cannot start this update now.", before
            )
        entry_image = copy.deepcopy(
            (dict(self.manager.entry.data), dict(self.manager.entry.options))
        )
        self._pin(owner, before, entry_image)
        source = await self._io(component_source, owner.repository.content.path.local)
        prepared = install_service(
            owner.access, owner.repository, before["available_version"]
        )
        if prepared is None:
            self._fail(
                "SERVICE_UNAVAILABLE", "Core update.install is unavailable.", before
            )
        assert prepared is not None
        await owner.access.admit(owner.repository)
        await self._admit_update(actor)
        self._pin(owner, before, entry_image)
        receipt = await self._io(
            capture_code,
            source,
            self.directory,
            {
                "installed_version": before["installed_version"],
                "target_version": before["available_version"],
                "entity_id": owner.target.entity_id,
                "owner": owner.provider,
                "repository": owner.repository_name,
                "repository_id": str(owner.repository.data.id),
            },
        )
        await self._io(
            write_receipt, self.directory, {**receipt, "state": "dispatching"}
        )
        try:
            await NativeUpdates(self.manager, actor)._visible_target(
                owner.target.entity_id
            )
            await self._admit_update(actor)
            self._pin(owner, before, entry_image)
            check_mutation_admission(actor)
            pin_install_service(owner.access, owner.repository, prepared)
        except (Exception, asyncio.CancelledError):
            await self._io(write_receipt, self.directory, receipt)
            raise
        return await self._dispatch(owner, actor, prepared, receipt)

    async def _dispatch(
        self,
        owner: SelfUpdateOwner,
        actor: CallContext,
        prepared: tuple[Any, dict[str, Any], Any],
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            await call_install_service(owner.access, prepared)
            await authorize_actor(actor, self.manager.backend.authorize)
            owner.check(self.manager.hass)
            if (
                owner.target.entity.installed_version != prepared[1]["version"]
                or owner.target.entity.in_progress
            ):
                raise RuntimeError("The owner did not confirm the requested version")
            receipt = {**receipt, "state": "installed"}
            await self._io(write_receipt, self.directory, receipt)
        except (Exception, asyncio.CancelledError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "The installation was dispatched but could not be verified. Inspect the owner before any further write; no retry occurred.",
                status="outcome_unknown",
            ) from error
        return {
            **self._describe(owner),
            "status": "pending_restart",
            "recovery": recovery_summary(receipt),
            "warnings": [
                "Installed files are newer than the running code. Restart Home Assistant, reconnect and check again. The recovery image covers component files, not dependencies or data."
            ],
        }

    @staticmethod
    def _fail(
        code: str, message: str, result: dict[str, Any], status: str = "not_started"
    ) -> None:
        raise ToolFailure(
            code,
            message,
            data={
                **result,
                "success": False,
                "error": {"code": code, "message": message},
            },
            status=status,
        )
