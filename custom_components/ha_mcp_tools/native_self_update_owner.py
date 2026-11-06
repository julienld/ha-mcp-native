"""Pin the HA-MCP repository, reusing the store's live update-entity resolver."""

from dataclasses import dataclass
from typing import Any

from ha_mcp.tool_api import CallContext, ToolFailure

from .native_operations.hacs_access import HacsAccess
from .native_operations.hacs_updates import repository_update_target
from .native_operations.hacs_writes import NativeHacs
from .native_operations.update_entities import UpdateTarget, check_update_target
from .native_self_update_provenance import paired_fork_origin, valid_repository

_REPOSITORIES = {"homeassistant-ai/ha-mcp", "homeassistant-ai/ha-mcp-integration"}


@dataclass(frozen=True)
class SelfUpdateOwner:
    target: UpdateTarget
    access: HacsAccess
    repository: Any
    repository_name: str
    provenance_verified: bool

    @property
    def provider(self) -> str:
        return self.access.domain

    @property
    def owner(self) -> Any:
        return self.access.owner

    def check(self, hass: Any) -> None:
        self.access.pin(self.repository)
        check_update_target(hass, self.target)
        current = repository_update_target(self.access, self.repository)
        if (
            current is None
            or current.entity is not self.target.entity
            or current.unique_id != self.target.unique_id
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The installation owner changed.",
                status="not_started",
            )
        if (
            not _is_self(self.repository)
            or self.repository.data.full_name != self.repository_name
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The repository provenance changed.",
                status="not_started",
            )


def _is_self(repository: Any) -> bool:
    return (
        repository.data.domain == "ha_mcp_tools"
        and valid_repository(repository.data.full_name)
        and repository.data.category == "integration"
        and repository.data.installed
    )


async def find_self_update(
    manager: Any, actor: CallContext, hidden: set[str]
) -> SelfUpdateOwner | None:
    if not {"hacs", "marketplace"}.intersection(manager.hass.config.components):
        return None
    access = await NativeHacs(manager, actor).access()
    matches = []
    for repository in access.repositories.list_all:
        if not _is_self(repository):
            continue
        access.remember(repository)
        access.pin(repository)
        target = repository_update_target(access, repository)
        if target is not None and target.entity_id not in hidden:
            name = repository.data.full_name
            verified = (
                name in _REPOSITORIES
                or await manager.hass.async_add_executor_job(
                    paired_fork_origin, name, str(target.entity.installed_version)
                )
            )
            access.pin(repository)
            matches.append(SelfUpdateOwner(target, access, repository, name, verified))
    if len(matches) > 1:
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "Multiple installation owners claim HA-MCP; resolve the duplicate before updating.",
            status="not_started",
        )
    return matches[0] if matches else None
