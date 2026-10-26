"""Pin the installed HACS owner and repository images without initializing HACS.

Owner contract researched against official HACS 2.0.5, commit
c0dfd8b44297c3673c21973e2539375a53687a9c. Validators come from the running
integration; this module never imports or constructs a HACS/client instance.
"""

import asyncio
import copy
from dataclasses import dataclass, field
from typing import Any, ClassVar

from homeassistant.config_entries import ConfigEntryState

from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import json_values_equal

from .command_validation import _validated_payload
from .registry_access import Authorize, admit_registry_write, authorize_actor


def repository_image(repository: Any) -> dict[str, Any]:
    return copy.deepcopy(
        {
            "data": repository.data.to_json(),
            "last_fetched": repository.data.last_fetched.isoformat()
            if repository.data.last_fetched
            else None,
            "path": repository.content.path.local,
            "ref": repository.ref,
            "pending_restart": repository.pending_restart,
        }
    )


def hacs_owner(hass: Any, *, writes: bool = False) -> Any:
    if "hacs" not in hass.config.components:
        raise ToolFailure(
            "COMPONENT_NOT_INSTALLED", "HACS is not loaded", status="not_started"
        )
    owner = hass.data.get("hacs")
    if owner is None or isinstance(owner, asyncio.Future):
        raise ToolFailure(
            "SERVICE_UNAVAILABLE",
            "HACS has not finished initializing",
            status="not_started",
        )
    entry = owner.configuration.config_entry
    if (
        entry is None
        or hass.config_entries.async_get_entry(entry.entry_id) is not entry
        or entry.state is not ConfigEntryState.LOADED
        or owner.status.startup
        or not owner.system.running
        or owner.data is None
    ):
        raise ToolFailure(
            "SERVICE_UNAVAILABLE",
            "The loaded HACS owner is not ready",
            status="not_started",
        )
    if writes and owner.system.disabled:
        raise ToolFailure(
            "SERVICE_UNAVAILABLE",
            f"HACS is disabled: {owner.system.disabled_reason}",
            status="not_started",
        )
    return owner


@dataclass
class HacsAccess:
    domain: ClassVar[str] = "hacs"
    module_root: ClassVar[str] = "custom_components.hacs"
    hass: Any
    actor: CallContext
    authorize: Authorize
    owner: Any
    repositories: Any
    storage: Any
    entry: Any
    github: Any
    githubapi: Any
    configuration: Any
    version: str
    common: Any
    entry_image: dict[str, Any]
    target_ids: dict[int, str] = field(default_factory=dict)
    target_names: dict[int, tuple[Any, ...]] = field(default_factory=dict)

    @staticmethod
    def get_owner(hass: Any, *, writes: bool = False) -> Any:
        from .marketplace_access import store_domain

        if store_domain(hass) != "hacs":
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "HACS owner changed to Marketplace",
                status="not_started",
            )
        return hacs_owner(hass, writes=writes)

    def guard(self, action: str, repository: Any = None) -> None:
        """Provider admission in addition to the live administrator check."""

    def category(self, category: str | None) -> None:
        """HACS retains its own available-category validator."""

    def command(self, action: str) -> str:
        suffix = {
            "search": "repositories/list",
            "info": "repository/info",
            "download": "repository/download",
            "remove": "repository/remove",
            "update_information": "repository/refresh",
            "add_repository": "repositories/add",
        }[action]
        return f"{self.domain}/{suffix}"

    def project(self, repository: Any) -> dict[str, Any]:
        from .hacs_projection import repository_data

        return repository_data(self.owner, repository)

    def cache(self, repository: Any = None) -> dict[str, Any]:
        from .hacs_projection import cache_status

        return cache_status(self.owner, repository)

    def listed(self, repository: Any) -> bool:
        return not repository.ignored_by_country_configuration

    @classmethod
    async def open(
        cls,
        hass: Any,
        actor: CallContext,
        authorize: Authorize,
        *,
        writes: bool = False,
    ) -> "HacsAccess":
        if writes:
            await admit_registry_write(actor, authorize)
        else:
            await authorize_actor(actor, authorize)
        owner = cls.get_owner(hass, writes=writes)
        return cls(
            hass,
            actor,
            authorize,
            owner,
            owner.repositories,
            owner.data,
            owner.configuration.config_entry,
            getattr(owner, "github", None),
            owner.githubapi,
            owner.configuration,
            str(owner.version),
            owner.common,
            copy.deepcopy(
                {
                    "data": dict(owner.configuration.config_entry.data),
                    "options": dict(owner.configuration.config_entry.options),
                }
            ),
        )

    def pin(
        self,
        repository: Any = None,
        before: dict[str, Any] | None = None,
        *,
        writes: bool = False,
    ) -> None:
        owner = self.get_owner(self.hass, writes=writes)
        if (
            owner is not self.owner
            or owner.repositories is not self.repositories
            or owner.data is not self.storage
            or owner.configuration is not self.configuration
            or owner.configuration.config_entry is not self.entry
            or getattr(owner, "github", None) is not self.github
            or owner.githubapi is not self.githubapi
            or str(owner.version) != self.version
            or owner.common is not self.common
            or not json_values_equal(
                {"data": dict(self.entry.data), "options": dict(self.entry.options)},
                self.entry_image,
            )
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "HACS owner changed during the operation",
                status="not_started",
            )
        if repository is not None:
            identifier = self.target_ids.get(id(repository))
            if (
                identifier is None
                or str(repository.data.id) != identifier
                or self.repositories.get_by_id(identifier) is not repository
                or getattr(repository, self.domain) is not owner
                or self.target_names.get(id(repository))
                != (
                    repository.data.full_name,
                    repository.data.category,
                    repository.data.domain,
                )
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "HACS repository owner changed",
                    status="not_started",
                )
            if before is not None and not json_values_equal(
                repository_image(repository), before
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "HACS repository changed during preparation",
                    status="not_started",
                )

    async def admit(
        self, repository: Any = None, before: dict[str, Any] | None = None
    ) -> None:
        await admit_registry_write(self.actor, self.authorize)
        self.pin(repository, before, writes=True)

    async def observe(
        self, repository: Any = None, before: dict[str, Any] | None = None
    ) -> None:
        await authorize_actor(self.actor, self.authorize)
        self.pin(repository, before)

    def validate(
        self, command: str, values: dict[str, Any]
    ) -> tuple[dict[str, Any], Any]:
        registration = self.hass.data.get("websocket_api", {}).get(command)
        payload = _validated_payload(self.hass, command, values)
        return payload, registration

    def pin_validator(self, command: str, registration: Any) -> None:
        if self.hass.data.get("websocket_api", {}).get(command) is not registration:
            raise ToolFailure(
                "CONFIG_CONFLICT", "The HACS validator changed", status="not_started"
            )

    def find(self, identifier: str) -> Any:
        repository = (
            self.repositories.get_by_full_name(identifier)
            if "/" in identifier
            else self.repositories.get_by_id(identifier)
        )
        if repository is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND",
                f"Repository '{identifier}' not found in HACS; it is not registered",
                status="not_started",
            )
        self.remember(repository)
        return repository

    def remember(self, repository: Any) -> None:
        self.target_ids[id(repository)] = str(repository.data.id)
        self.target_names[id(repository)] = (
            repository.data.full_name,
            repository.data.category,
            repository.data.domain,
        )


def check_self_files(owner: Any, repository: Any, manager_domain: str) -> bool:
    self_affecting = (
        repository.data.category == "integration"
        and repository.data.domain in {manager_domain, "hacs"}
    )
    if self_affecting and str(owner.version) != "2.0.5":
        raise ToolFailure(
            "UNSUPPORTED_OPERATION",
            "Self-affecting HACS file changes require a reviewed owner lifecycle; this HACS version has not been qualified",
            status="not_started",
        )
    return self_affecting
