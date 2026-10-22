"""Check the real edit-snapshot target and values, including renamed entities."""

from typing import Any

from ha_mcp.tool_api import CallContext


async def guard_snapshot_visibility(
    manager: Any,
    actor: CallContext,
    action: str,
    data: dict[str, Any],
    current: Any = None,
) -> None:
    visibility = getattr(manager, "visibility", None)
    if visibility is None:
        return
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(manager.hass)
    domain, identifier = data.get("domain", ""), data.get("entity_id", "")
    config = data.get("config")
    entity_ids: list[str] = []
    if domain == "entity_registry":
        entry = registry.async_get(identifier)
        if entry is not None:
            entity_ids.append(entry.entity_id)
    elif domain == "device":
        entity_ids = [
            entry.entity_id
            for entry in er.async_entries_for_device(registry, identifier)
        ]
    elif domain == "integration":
        entity_ids = [
            entry.entity_id
            for entry in er.async_entries_for_config_entry(registry, identifier)
        ]
    elif domain.startswith("helper_") and isinstance(config, dict):
        if config.get("entry_id"):
            entity_ids = [
                entry.entity_id
                for entry in er.async_entries_for_config_entry(
                    registry, config["entry_id"]
                )
            ]
        elif config.get("id"):
            helper_type = domain.removeprefix("helper_")
            entity_id = registry.async_get_entity_id(
                helper_type, helper_type, str(config["id"])
            )
            if entity_id:
                entity_ids.append(entity_id)
    await visibility.before(
        "ha_manage_backup",
        {
            "scope": "edits",
            "action": action,
            "snapshot": data,
            "current": current,
            "entity_ids": entity_ids,
        },
        actor,
    )
    await manager.backend.authorize(actor)
