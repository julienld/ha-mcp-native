"""Render through the existing Core domain owner, with Core input validation."""

from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.errors import create_validation_error
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation

from ..helper_collections import _INVALID_ERRORS


async def render_blueprint(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    from homeassistant.components.blueprint.errors import BlueprintException
    from homeassistant.components.blueprint.models import DomainBlueprints
    from homeassistant.helpers import config_validation as cv
    from homeassistant.util.yaml import UndefinedSubstitution

    domain = arguments["domain"]
    owner = hass.data.get("blueprint", {}).get(domain)
    if (
        not isinstance(owner, DomainBlueprints)
        or owner.domain != domain
        or owner.hass is not hass
    ):
        raise UnsupportedOperation("blueprints.render.owner")
    try:
        path = cv.path(arguments["path"])
    except _INVALID_ERRORS as error:
        data = create_validation_error(
            str(error), context={"domain": domain, "path": arguments["path"]}
        )
        raise ToolFailure(
            data["error"]["code"], data["error"]["message"], data=data
        ) from error
    try:
        inputs = await owner.async_inputs_from_config(
            {"use_blueprint": {"path": path, "input": arguments["input"]}}
        )
        config = inputs.async_substitute()
    except (BlueprintException, UndefinedSubstitution) as error:
        return {"success": False, "error": str(error)}
    return {"success": True, "result": {"substituted_config": config}}
