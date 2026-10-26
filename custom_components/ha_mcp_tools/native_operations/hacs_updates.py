"""Resolve repository updates through Core identity, independent of entity names."""

import copy
from typing import Any

from homeassistant.core import Context

from ha_mcp.tool_api import ToolFailure

from .hacs_access import HacsAccess
from .update_entities import UpdateTarget, check_update_target, update_target


def repository_update_target(
    access: HacsAccess, repository: Any
) -> UpdateTarget | None:
    """Return the live provider entity or None before dispatch if none exists.

    Consumers such as self-update can reuse this resolution after pinning their
    exact repository/domain provenance. Entity names are never evidence.
    """
    if "update" not in access.hass.config.components:
        return None
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(access.hass)
    entity_id = registry.async_get_entity_id(
        "update", access.domain, str(repository.data.id)
    )
    if entity_id is None:
        return None
    entry = registry.async_get(entity_id)
    target = update_target(access.hass, entity_id)
    if (
        entry is None
        or entry.config_entry_id != access.entry.entry_id
        or entry.platform != access.domain
        or str(entry.unique_id) != str(repository.data.id)
        or target.entity.repository is not repository
        or getattr(target.entity, access.domain, None) is not access.owner
    ):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "Update entity does not belong to the pinned store repository",
            status="not_started",
        )
    return target


def install_service(
    access: HacsAccess, repository: Any, version: str | None
) -> tuple[Any, dict[str, Any], UpdateTarget] | None:
    target = repository_update_target(access, repository)
    if target is None:
        return None
    service = access.hass.services.async_services_for_domain("update").get("install")
    if service is None or not callable(service.schema):
        raise ToolFailure(
            "UNSUPPORTED_OPERATION",
            "Core update.install is unavailable for this repository",
            status="not_started",
        )
    values = {"entity_id": target.entity_id}
    if version is not None:
        values["version"] = version
    service.schema(copy.deepcopy(values))
    return service, values, target


def pin_install_service(
    access: HacsAccess,
    repository: Any,
    prepared: tuple[Any, dict[str, Any], UpdateTarget],
) -> None:
    service, _values, target = prepared
    check_update_target(access.hass, target)
    current = repository_update_target(access, repository)
    if (
        current is None
        or current.entity is not target.entity
        or current.entity_id != target.entity_id
        or access.hass.services.async_services_for_domain("update").get("install")
        is not service
    ):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "Core update.install target changed before dispatch",
            status="not_started",
        )


async def call_install_service(
    access: HacsAccess, prepared: tuple[Any, dict[str, Any], UpdateTarget]
) -> None:
    await access.hass.services.async_call(
        "update",
        "install",
        copy.deepcopy(prepared[1]),
        blocking=True,
        context=Context(user_id=access.actor.user_id),
    )
