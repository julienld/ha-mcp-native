"""Pin the real UpdateEntity and its published state on the owning Core loop."""

from dataclasses import dataclass, field
from typing import Any

from ha_mcp.tool_api import ToolFailure, UnsupportedOperation


@dataclass(frozen=True, slots=True)
class UpdateTarget:
    entity_id: str
    unique_id: str | None
    component: Any = field(repr=False)
    entity: Any = field(repr=False)


def update_target(hass: Any, entity_id: str) -> UpdateTarget:
    from homeassistant.components.update import DATA_COMPONENT, UpdateEntity

    component = hass.data.get(DATA_COMPONENT)
    if component is None:
        raise UnsupportedOperation("Core update integration is not loaded")
    entity = component.get_entity(entity_id)
    if not isinstance(entity, UpdateEntity) or hass.states.get(entity_id) is None:
        raise ToolFailure(
            "ENTITY_NOT_FOUND", "Update entity not found", status="not_started"
        )
    return UpdateTarget(entity_id, entity.unique_id, component, entity)


def check_update_target(hass: Any, target: UpdateTarget) -> None:
    current = update_target(hass, target.entity_id)
    if (
        current.component is not target.component
        or current.entity is not target.entity
        or current.unique_id != target.unique_id
        or current.entity.entity_id != target.entity_id
    ):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "The Core update target changed during the operation.",
            status="not_started",
        )


def core_update_category(hass: Any, entity: Any) -> str | None:
    if "hassio" not in hass.config.components:
        return None
    from homeassistant.components.hassio.update import (
        SupervisorCoreUpdateEntity,
        SupervisorOSUpdateEntity,
        SupervisorSupervisorUpdateEntity,
    )

    for owner, category in (
        (SupervisorCoreUpdateEntity, "core"),
        (SupervisorOSUpdateEntity, "os"),
        (SupervisorSupervisorUpdateEntity, "supervisor"),
    ):
        if isinstance(entity, owner):
            return category
    return None


def stopping_update(hass: Any, target: UpdateTarget) -> bool:
    return core_update_category(hass, target.entity) in {"core", "os"}


def protected_update(hass: Any, target: UpdateTarget) -> bool:
    return core_update_category(hass, target.entity) is not None
