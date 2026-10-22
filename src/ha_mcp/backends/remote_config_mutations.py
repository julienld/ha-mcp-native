"""Config mutations retain existing HTTP clients and compare confirmed targets."""

import importlib
from typing import Any

from ha_mcp.tool_api import ToolFailure, UnsupportedOperation

CONFIG_MUTATION_OPERATIONS = frozenset(
    {f"config.{domain}.delete" for domain in ("automation", "script", "scene")}
    | {"config.backup.capture"}
)


async def execute_config_mutation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    from ha_mcp.client.rest_client import HomeAssistantError, SceneResolution
    from ha_mcp.tools.helpers import exception_to_structured_error

    from .remote_config_ops import execute_config_operation

    if operation not in CONFIG_MUTATION_OPERATIONS:
        raise UnsupportedOperation(operation)
    if operation == "config.backup.capture":
        return await _capture_backup(client, arguments)
    domain = operation.split(".")[1]
    identifier = arguments["identifier"]
    resolution = arguments.get("resolution")
    try:
        current = await execute_config_operation(
            client,
            f"config.{domain}.read",
            {"identifier": identifier, "resolution": resolution},
        )
        key = (
            current.get("storage_key")
            or (current.get("config") or {}).get("id")
            or identifier
        )
        if (
            not current["found"]
            or key != arguments["storage_key"]
            or current["config"] != arguments["expected_config"]
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The config changed after its snapshot was read; no mutation was sent.",
                status="not_started",
            )
        if domain == "scene":
            resolved = (
                SceneResolution(**resolution)
                if resolution is not None
                else await client._resolve_scene(identifier)
            )
            result: dict[str, Any] = await client.delete_scene_config(
                identifier, resolution=resolved
            )
        else:
            result = await getattr(client, f"delete_{domain}_config")(
                identifier, resolved_id=arguments["storage_key"]
            )
        return result
    except HomeAssistantError as error:
        data = exception_to_structured_error(error, raise_error=False)
        raise ToolFailure(
            data["error"]["code"], data["error"]["message"], data=data
        ) from error


async def _capture_backup(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from ha_mcp.config import get_global_settings
    from ha_mcp.tools.auto_backup import _capture_pre_write_snapshot

    domain, source = arguments["domain"], arguments["source_tool"]
    classes = {
        "automation": "AutomationConfigTools",
        "script": "ConfigScriptTools",
        "scene": "ConfigSceneTools",
    }
    if domain not in classes or source not in {
        f"ha_config_set_{domain}",
        f"ha_config_remove_{domain}",
    }:
        raise UnsupportedOperation("config.backup.capture.domain")
    settings = get_global_settings()
    if not settings.enable_auto_backup:
        return {}
    owner = getattr(
        importlib.import_module(f"ha_mcp.tools.tools_config_{domain}s"), classes[domain]
    )
    param = "identifier" if domain == "automation" else f"{domain}_id"
    warning = await _capture_pre_write_snapshot(
        getattr(owner, source),
        (),
        {param: arguments["backup_id"]},
        settings=settings,
        explicit_client=client,
        domain=domain,
        domain_fn=None,
        id_param=param,
        id_fn=None,
        mandatory=False,
        domain_resolver=None,
    )
    return {"warnings": [warning]} if warning else {}
