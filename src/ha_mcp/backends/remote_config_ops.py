"""Fresh config-read operations backed by the existing remote clients."""

from __future__ import annotations

from typing import Any

from ha_mcp.tool_api import ToolFailure, UnsupportedOperation

CONFIG_READ_OPERATIONS = frozenset(
    {
        "config.automation.read",
        "config.script.read",
        "config.scene.read",
        "config.scene.resolve",
        "config.entity_id",
        "entities.category.read",
        "zones.snapshot",
        "scenes.inventory",
    }
)


async def _read_item(
    client: Any, operation: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    from ha_mcp.client.rest_client import (
        HomeAssistantAPIError,
        SceneStorageConfigNotFoundError,
    )
    from ha_mcp.tools.tools_config_automations import AutomationConfigTools
    from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

    identifier = arguments["identifier"]
    domain = operation.split(".")[1]
    try:
        if operation == "config.scene.resolve":
            resolution = await client._resolve_scene(arguments["identifier"])
            return {
                "storage_key": resolution.storage_key,
                "registry_hit": resolution.registry_hit,
                "platform": resolution.platform,
            }
        if domain == "scene" and arguments.get("resolution") is not None:
            from ha_mcp.client.rest_client import SceneResolution

            value = await client.get_scene_config(
                identifier, resolution=SceneResolution(**arguments["resolution"])
            )
        else:
            value = await getattr(client, f"get_{domain}_config")(identifier)
    except SceneStorageConfigNotFoundError as error:
        return {
            "found": False,
            "not_in_storage": True,
            "platform": error.platform,
            "storage_key": error.storage_key,
        }
    except HomeAssistantAPIError as error:
        if error.status_code != 404:
            raise
        if domain == "automation":
            available = await AutomationConfigTools(
                client
            )._list_automation_entity_ids()
        elif domain == "script":
            available = await ConfigScriptTools(client)._list_script_entity_ids()
        else:
            available = []
        return {"found": False, "available_ids": available, "not_in_storage": False}
    if domain == "automation":
        return {"found": True, "config": value}
    if not isinstance(value, dict):
        return {"found": True, "config": value, "storage_key": None}
    return {
        "found": True,
        "config": value.get("config", value),
        "storage_key": value.get(f"{domain}_id"),
    }


async def execute_config_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    from ha_mcp.client.rest_client import HomeAssistantError
    from ha_mcp.tools.helpers import exception_to_structured_error

    try:
        if operation == "scenes.inventory":
            from ha_mcp.tools.scene_discovery import _inventory

            return await _inventory(client)
        if operation == "zones.snapshot":
            from ha_mcp.tools.tools_zones import ZoneTools

            return await ZoneTools(client)._zone_snapshot()
        if operation in {
            "config.automation.read",
            "config.script.read",
            "config.scene.read",
            "config.scene.resolve",
        }:
            return await _read_item(client, operation, arguments)
        if operation == "config.entity_id":
            if arguments["domain"] == "script":
                from ha_mcp.tools.entity_registration import (
                    resolve_entity_id_after_write,
                )

                return await resolve_entity_id_after_write(
                    client, arguments["identifier"], "script", timeout=0
                )
            if arguments["domain"] == "automation":
                from ha_mcp.tools.tools_config_automations import AutomationConfigTools

                return await AutomationConfigTools(
                    client
                )._resolve_automation_entity_id(arguments["identifier"])
            from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

            return await ConfigSceneTools(client)._resolve_scene_entity_id(
                arguments["identifier"]
            )
        if operation == "entities.category.read":
            from ha_mcp.tools.config_write_helpers import fetch_entity_category

            warnings: list[str] = []
            category = await fetch_entity_category(
                client, arguments["entity_id"], arguments["scope"], warnings
            )
            return {"category": category, "warnings": warnings}
    except (HomeAssistantError, TimeoutError, OSError) as error:
        data = exception_to_structured_error(error, raise_error=False)
        failure = data["error"]
        raise ToolFailure(failure["code"], failure["message"], data=data) from error
    raise UnsupportedOperation(operation)
