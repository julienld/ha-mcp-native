"""Registry semantic operations using the existing remote acquisition paths."""

from __future__ import annotations

from typing import Any

from ha_mcp.tool_api import UnsupportedOperation

REGISTRY_OPERATIONS = frozenset({"registries.snapshot", "assist.exposure"})


async def execute_registry_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    """Keep capability negotiation and read-only fallbacks in the remote backend."""
    if operation == "registries.snapshot" and arguments == {
        "registries": ["area", "floor"]
    }:
        from ha_mcp.tools.tools_areas import AreaTools

        areas, floors = await AreaTools(client)._fetch_area_floor_registries({})
        return {"areas": areas, "floors": floors}
    if operation == "registries.snapshot" and arguments == {"registries": ["label"]}:
        from ha_mcp.tools.tools_labels import LabelTools

        return {"labels": await LabelTools(client)._list_labels()}
    if operation == "registries.snapshot" and arguments.get("registries") == [
        "category"
    ]:
        from ha_mcp.tools.tools_categories import CategoryTools

        scopes = arguments.get("category_scopes")
        if isinstance(scopes, list) and len(scopes) == 1 and isinstance(scopes[0], str):
            categories = await CategoryTools(client)._list_categories(scopes[0], None)
            return {"categories": {scopes[0]: categories}}
    if operation == "assist.exposure":
        from ha_mcp.tools.tools_voice_assistant import VoiceAssistantTools

        return await VoiceAssistantTools(client)._fetch_exposure_payload(
            arguments.get("entity_id")
        )
    raise UnsupportedOperation(operation)
