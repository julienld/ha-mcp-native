"""Read-only code-mode observations for the real Core qualification bench.

Root hook: GET code_probe -> code_probe(hass, request['hass_user'].id).
"""

from typing import Any


def code_probe(hass: Any, request_user_id: str) -> dict[str, Any]:
    manager = hass.data.get("ha_mcp_tools", {}).get("manager")
    state = hass.states.get("input_boolean.native_light")
    return {
        "available": bool(
            manager
            and manager.engine
            and "ha_manage_custom_tool" in manager.engine.tools
        ),
        "saved_path": hass.config.path("native-contract-saved-code.json"),
        "state": state.state if state is not None else None,
        "state_user_id": state.context.user_id if state is not None else None,
        "request_user_id": request_user_id,
    }
