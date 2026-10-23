"""Remote blueprint substitution facade over the shared rendering policy."""

from typing import Any, cast

from ..backends.legacy import legacy_errors, legacy_errors_sync
from ..runtime.blueprint_substitute import TakenControl, rendered_blueprint
from ..runtime.blueprint_substitute import (
    raise_substitute_failure as _raise_substitute_failure,
)
from ..runtime.blueprint_substitute import take_control_config as _take_control_config
from ..runtime.blueprint_substitute import validate_write_modes as _validate_write_modes

raise_substitute_failure = legacy_errors_sync(_raise_substitute_failure)
validate_write_modes = legacy_errors_sync(_validate_write_modes)


@legacy_errors
async def substitute_blueprint(
    client: Any, domain: str, path: str, blueprint_input: dict[str, Any]
) -> dict[str, Any]:
    response = await client.send_websocket_message(
        {
            "type": "blueprint/substitute",
            "domain": domain,
            "path": path,
            "input": blueprint_input,
        }
    )
    return rendered_blueprint(domain, path, response)


@legacy_errors
async def take_control_config(
    client: Any, domain: str, identifier: str, current_config: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    async def render(domain: str, path: str, inputs: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], await substitute_blueprint(client, domain, path, inputs)
        )

    return await _take_control_config(render, domain, identifier, current_config)


__all__ = [
    "TakenControl",
    "substitute_blueprint",
    "take_control_config",
]
