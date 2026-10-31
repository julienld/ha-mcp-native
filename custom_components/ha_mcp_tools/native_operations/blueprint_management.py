"""Blueprint lifecycle through Core's domain stores, importer and consumer owners."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from typing import Any

from ha_mcp.runtime.blueprint_substitute import raise_substitute_failure
from ha_mcp.runtime.blueprint_values import (
    blueprint_get_result,
    blueprint_imported_result,
    blueprint_list_result,
    blueprint_saved_result,
    normalize_blueprint_path,
    parse_blueprint_body,
)
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from ..blueprint_files import InstalledBlueprint
from ..helper_collections import _INVALID_ERRORS
from .blueprint_owner import BlueprintOwner
from .registry_access import (
    Authorize,
    Capture,
    admit_registry_write,
    authorize_actor,
    registry_failure,
    registry_json,
)
from .registry_failures import with_recovery_hint


@dataclass(slots=True)
class NativeBlueprints:
    hass: Any
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)
    backup: Capture | None = field(default=None, kw_only=True, repr=False)

    @staticmethod
    def _validate_domain(domain: str) -> None:
        if domain not in {"automation", "script"}:
            failure = ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                f"Invalid domain {domain!r}: blueprint domain must be automation or script",
                status="not_started",
            )
            failure.data.update(domain=domain, valid_domains=["automation", "script"])
            raise failure

    def store(self, domain: str) -> BlueprintOwner:
        self._validate_domain(domain)
        return BlueprintOwner(self.hass, self.actor, domain, authorize=self.authorize)

    @staticmethod
    def required(value: str | None, name: str, action: str) -> str:
        if not value:
            raise ToolFailure(
                "VALIDATION_MISSING_PARAMETER",
                f"'{name}' is required for action='{action}'.",
                status="not_started",
            )
        return value

    @staticmethod
    def validated(value: str, kind: str) -> str:
        from homeassistant.helpers import config_validation as cv

        try:
            validator = cv.path if kind == "path" else cv.url
            result: str = validator(value)
            return result
        except _INVALID_ERRORS as error:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER", str(error), status="not_started"
            ) from error

    async def manage(
        self,
        action: str,
        domain: str = "automation",
        path: str | None = None,
        url: str | None = None,
        yaml: str | None = None,
        source_url: str | None = None,
        overwrite: bool = False,
        input: dict[str, Any] | None = None,  # noqa: A002 - Core public key
        confirm: bool = False,
    ) -> dict[str, Any]:
        await authorize_actor(self.actor, self.authorize)
        if action == "import":
            await admit_registry_write(self.actor, self.authorize)
            return await self.import_url(
                self.validated(self.required(url, "url", action), "url"), overwrite
            )
        self._validate_domain(domain)
        if action == "list" and domain not in self.hass.data.get("blueprint", {}):
            return blueprint_list_result({}, domain)
        store = self.store(domain)
        try:
            if action == "list":
                return blueprint_list_result(await self.entries(store), domain)
            path = self.validated(self.required(path, "path", action), "path")
            if action == "get":
                return await self.get(store, path)
            if action == "substitute":
                return await self.substitute(store, path, input or {})
            if action == "delete":
                return await self.remove(store, path, confirm=confirm)
            if action != "save":
                raise ToolFailure(
                    "VALIDATION_INVALID_PARAMETER",
                    "Unknown blueprint action",
                    status="not_started",
                )
            source_url = (
                self.validated(source_url, "url") if source_url is not None else None
            )
            blueprint = await self.prepare(
                store, self.required(yaml, "yaml", action), source_url
            )
            return await self.save(
                store, normalize_blueprint_path(path), blueprint, overwrite
            )
        except Exception as error:
            raise store.failure(error) from error

    async def entries(self, store: BlueprintOwner) -> dict[str, Any]:
        from homeassistant.components.blueprint.models import Blueprint

        await authorize_actor(self.actor, self.authorize)
        store.check_owner()
        values = await await_owned_io(store.owner.async_get_blueprints())
        await authorize_actor(self.actor, self.authorize)
        store.check_owner()
        return registry_json(
            {
                path: {"metadata": value.metadata}
                if isinstance(value, Blueprint)
                else {"error": str(value)}
                for path, value in values.items()
            }
        )

    async def installed(self, store: BlueprintOwner, path: str) -> dict[str, Any]:
        entries = await self.entries(store)
        if path not in entries:
            raise with_recovery_hint(
                ToolFailure(
                    "RESOURCE_NOT_FOUND",
                    f"Blueprint not found: {path}",
                    status="not_started",
                ),
                "Use ha_manage_blueprints(action='list', domain=...) to find an installed blueprint path",
            )
        entry: dict[str, Any] = entries[path]
        return entry

    def consumers(self, domain: str, path: str) -> list[str]:
        from homeassistant.components import automation, script

        lookup = (
            automation.automations_with_blueprint
            if domain == "automation"
            else script.scripts_with_blueprint
        )
        return sorted(set(lookup(self.hass, path)))

    async def get(self, store: BlueprintOwner, path: str) -> dict[str, Any]:
        result, _source = blueprint_get_result(
            store.domain, path, await self.installed(store, path)
        )
        observed = await store.read(path)
        if observed.text is not None:
            body = await store.io(parse_blueprint_body, observed.text)
            if body is not None:
                result["data"]["config"] = body
            result["data"].update(yaml=observed.text, yaml_source="file")
        else:
            result["warnings"] = [
                "The installed blueprint file was not readable; no source URL was substituted for its contents."
            ]
        result["data"]["used_by"] = self.consumers(store.domain, path)
        return result

    async def substitute(
        self, store: BlueprintOwner, path: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        from homeassistant.components.blueprint.errors import BlueprintException
        from homeassistant.util.yaml import UndefinedSubstitution

        try:
            inputs = await await_owned_io(
                store.owner.async_inputs_from_config(
                    {"use_blueprint": {"path": path, "input": copy.deepcopy(values)}}
                )
            )
            await authorize_actor(self.actor, self.authorize)
            store.check_owner()
            config = inputs.async_substitute()
        except (BlueprintException, UndefinedSubstitution) as error:
            raise_substitute_failure(store.domain, path, {"error": str(error)})
        return {
            "success": True,
            "data": {
                "domain": store.domain,
                "path": path,
                "config": registry_json(config),
            },
        }

    async def capture(
        self, store: BlueprintOwner, path: str, before: InstalledBlueprint
    ) -> list[str]:
        if self.backup is None or before.text is None:
            return []
        captured = await self.backup(
            {
                "domain": f"blueprint_{store.domain}",
                "path": path,
                "current_yaml": before.text,
                "source_tool": "ha_manage_blueprints",
            },
            self.actor,
        )
        return list(captured.get("warnings") or []) if captured else []

    @staticmethod
    async def prepare(store: BlueprintOwner, text: str, source_url: str | None) -> Any:
        from homeassistant.exceptions import HomeAssistantError

        try:
            return await store.parse(text, source_url)
        except HomeAssistantError as error:
            raise ToolFailure(
                "VALIDATION_FAILED", str(error), status="not_started"
            ) from error

    async def save(
        self, store: BlueprintOwner, path: str, blueprint: Any, overwrite: bool
    ) -> dict[str, Any]:
        async with registry_update_lock(f"blueprint_{store.domain}", path):
            await admit_registry_write(self.actor, self.authorize)
            before = await store.read(path)
            if before.text is not None and not overwrite:
                raise ToolFailure(
                    "RESOURCE_ALREADY_EXISTS",
                    f"Blueprint already exists at '{path}'. Pass overwrite=True to replace it.",
                    status="not_started",
                )
            warnings = await self.capture(store, path, before) if overwrite else []
            replaced = await store.write(path, before, blueprint, overwrite=overwrite)
        result = blueprint_saved_result(store.domain, path, replaced)
        if warnings:
            result["warnings"] = warnings
        return result

    async def remove(
        self, store: BlueprintOwner, path: str, *, confirm: bool
    ) -> dict[str, Any]:
        from homeassistant.components.blueprint.errors import BlueprintInUse

        if not confirm:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                f"Deletion not confirmed. Set confirm=True to delete blueprint '{path}'.",
                status="not_started",
            )
        async with registry_update_lock(f"blueprint_{store.domain}", path):
            await admit_registry_write(self.actor, self.authorize)
            await self.installed(store, path)
            before = await store.read(path)
            if before.text is None:
                raise with_recovery_hint(
                    ToolFailure(
                        "RESOURCE_NOT_FOUND",
                        f"Blueprint not found: {path}",
                        status="not_started",
                    ),
                    "Use ha_manage_blueprints(action='list', domain=...) to find an installed blueprint path",
                )
            warnings = await self.capture(store, path, before)
            try:
                await store.remove(path, before)
            except BlueprintInUse as error:
                consumers = self.consumers(store.domain, path)
                raise ToolFailure(
                    "RESOURCE_LOCKED",
                    f"Blueprint '{path}' is in use",
                    data={
                        "success": False,
                        "error": {
                            "code": "RESOURCE_LOCKED",
                            "message": f"Blueprint '{path}' is in use; remove its consumers first",
                        },
                        "domain": store.domain,
                        "path": path,
                        "in_use_by": consumers,
                    },
                    status="not_started",
                ) from error
        result: dict[str, Any] = {
            "success": True,
            "data": {
                "domain": store.domain,
                "path": path,
                "message": "Blueprint deleted.",
            },
        }
        if warnings:
            result["warnings"] = warnings
        return result

    async def import_url(self, url: str, overwrite: bool) -> dict[str, Any]:
        from homeassistant.components.blueprint.importer import fetch_blueprint_from_url

        async def fetch() -> Any:
            await authorize_actor(self.actor, self.authorize)
            async with asyncio.timeout(10):
                return await fetch_blueprint_from_url(self.hass, url)

        try:
            imported = await await_owned_io(fetch())
        except Exception as error:
            raise with_recovery_hint(
                registry_failure(error, {"action": "import", "url": url}),
                "Verify the blueprint URL is reachable and points to a supported blueprint source before importing again",
            ) from error
        await authorize_actor(self.actor, self.authorize)
        if imported is None:
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                "This blueprint URL is not supported",
                status="not_started",
            )
        metadata = registry_json(imported.blueprint.metadata)
        domain = metadata["domain"]
        store = self.store(domain)
        try:
            path = normalize_blueprint_path(
                self.validated(imported.suggested_filename, "path")
            )
            # Reuse Core's parser/validator/stamping exactly as a direct save;
            # never fetch the source again to capture the installed before-image.
            blueprint = await self.prepare(store, imported.raw_data, url)
            saved = await self.save(store, path, blueprint, overwrite)
            result = blueprint_imported_result(
                url, path, metadata, saved["data"]["overrides_existing"]
            )
            if "warnings" in saved:
                result["warnings"] = saved["warnings"]
            return result
        except Exception as error:
            raise store.failure(error) from error
