"""Keep blueprint filesystem writes, cache updates and reloads owned by Core."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io

from ..blueprint_files import InstalledBlueprint, read_installed_blueprint
from .registry_access import Authorize, admit_registry_write, authorize_actor


@dataclass(slots=True)
class BlueprintOwner:
    hass: Any
    actor: CallContext
    domain: str
    authorize: Authorize = field(kw_only=True, repr=False)
    owner: Any = field(init=False, repr=False)
    started: bool = field(default=False, init=False)
    completed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        from homeassistant.components.blueprint.models import DomainBlueprints

        self.owner = self.hass.data.get("blueprint", {}).get(self.domain)
        if (
            type(self.owner) is not DomainBlueprints
            or self.owner.hass is not self.hass
            or self.owner.domain != self.domain
        ):
            raise UnsupportedOperation("blueprints.domain_owner")

    def check_owner(self) -> None:
        if self.hass.data.get("blueprint", {}).get(self.domain) is not self.owner:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The blueprint domain owner changed",
                status="not_started",
            )

    async def io(self, function: Any, *args: Any) -> Any:
        await authorize_actor(self.actor, self.authorize)
        self.check_owner()
        result = await await_owned_io(self.hass.async_add_executor_job(function, *args))
        await authorize_actor(self.actor, self.authorize)
        self.check_owner()
        return result

    async def read(self, path: str) -> InstalledBlueprint:
        await authorize_actor(self.actor, self.authorize)
        self.check_owner()
        result: InstalledBlueprint = await self.io(
            read_installed_blueprint, self.owner.blueprint_folder, path
        )
        return result

    async def parse(self, text: str, source_url: str | None) -> Any:
        def prepare() -> Any:
            from homeassistant.components.blueprint.models import Blueprint
            from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
            from homeassistant.util import yaml

            blueprint = Blueprint(
                yaml.parse_yaml(text),
                expected_domain=self.domain,
                schema=BLUEPRINT_SCHEMA,
            )
            if source_url is not None:
                blueprint.update_metadata(source_url=source_url)
            if errors := blueprint.validate():
                raise ToolFailure(
                    "VALIDATION_FAILED",
                    "Blueprint failed validation: " + "; ".join(errors),
                    status="not_started",
                )
            return blueprint

        return await self.io(prepare)

    async def write(
        self, path: str, before: InstalledBlueprint, blueprint: Any, *, overwrite: bool
    ) -> bool:
        # Core's public API has no compare-and-write callback. This is an
        # optimistic preflight, not an atomic CAS against external file editors.
        expected_text = await self.io(blueprint.yaml)
        result = await await_owned_io(self._write(path, before, blueprint, overwrite))
        current = await self.read(path)
        if current.text != expected_text:
            raise self.unknown(
                "The saved blueprint could not be verified; inspect it before retrying"
            )
        return result

    async def _preflight(self, path: str, before: InstalledBlueprint) -> None:
        await admit_registry_write(self.actor, self.authorize)
        current = await self.read(path)
        if current != before:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Blueprint changed after its before-image",
                status="not_started",
            )
        await admit_registry_write(self.actor, self.authorize)
        self.check_owner()
        check_mutation_admission(self.actor)

    async def _write(
        self, path: str, before: InstalledBlueprint, blueprint: Any, overwrite: bool
    ) -> bool:
        from homeassistant.components.blueprint.errors import FileAlreadyExists

        await self._preflight(path, before)
        self.started = True
        try:
            result: bool = await self.owner.async_add_blueprint(
                blueprint, path, allow_override=overwrite
            )
        except FileAlreadyExists as error:
            if overwrite:
                # Core's pre-write existence gate runs only without override;
                # this exception may instead have come from the later reload.
                raise self.unknown(
                    "Blueprint overwrite failed after Core dispatch; inspect the file and consumers before retrying"
                ) from error
            self.started = False
            raise ToolFailure(
                "RESOURCE_ALREADY_EXISTS",
                "Blueprint already exists; use overwrite=True to replace it",
                status="not_started",
            ) from error
        except asyncio.CancelledError as error:
            raise self.unknown(
                "Core cancelled blueprint persistence or reload after dispatch; inspect it before retrying"
            ) from error
        self.completed = True
        return result

    async def remove(self, path: str, before: InstalledBlueprint) -> None:
        await await_owned_io(self._remove(path, before))
        if (await self.read(path)).text is not None:
            raise self.unknown(
                "Core completed the deletion, but a blueprint file is present; inspect it before retrying"
            )

    async def _remove(self, path: str, before: InstalledBlueprint) -> None:
        from homeassistant.components.blueprint.errors import BlueprintInUse

        await self._preflight(path, before)
        self.started = True
        try:
            await self.owner.async_remove_blueprint(path)
        except BlueprintInUse:
            self.started = False
            raise
        except asyncio.CancelledError as error:
            raise self.unknown(
                "Core cancelled blueprint deletion after dispatch; inspect it before retrying"
            ) from error
        self.completed = True

    def unknown(self, message: str) -> ToolFailure:
        return ToolFailure(
            "OUTCOME_UNKNOWN",
            message,
            data={
                "success": False,
                "error": {"code": "OUTCOME_UNKNOWN", "message": message},
                "write_committed": True if self.completed else None,
                "post_write_verified": False,
            },
            status="outcome_unknown",
        )

    def failure(self, error: Exception) -> ToolFailure:
        if self.started:
            return self.unknown(
                "A blueprint write started but did not complete normally; inspect its current file and consumers before retrying"
            )
        if isinstance(error, ToolFailure):
            if error.status is None:
                error.status = "not_started"
            return error
        return ToolFailure(
            "SERVICE_CALL_FAILED",
            f"Blueprint operation failed ({type(error).__name__})",
            status="not_started",
        )
