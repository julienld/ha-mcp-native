"""Pure action-to-service normalization shared with the native Core service."""

from typing import Any, ClassVar

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.utils.domain_handlers import get_domain_handler

from .errors import raise_failure


class DeviceActionValues:
    def _resolve_service_name(
        self,
        domain: str,
        action: str,
        parameters: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any] | None]:
        if domain == "climate" and action in ("heat", "cool", "auto", "heat_cool"):
            parameters = dict(parameters or {})
            parameters["hvac_mode"] = action
            return "set_hvac_mode", parameters

        service_name = self._SERVICE_OVERRIDES.get(
            (domain, action), self._GENERIC_SERVICE_MAP.get(action, action)
        )
        return service_name, parameters

    # Domain-specific action-to-service mappings; anything not listed
    # falls through to _GENERIC_SERVICE_MAP, then to the action verbatim.
    # tests/src/e2e/workflows/device_control/test_action_service_resolution.py
    # pins every DOMAIN_HANDLERS table entry through this resolver against
    # a live instance's service list.
    _SERVICE_OVERRIDES: ClassVar[dict[tuple[str, str], str]] = {
        ("cover", "open"): "open_cover",
        ("cover", "close"): "close_cover",
        ("cover", "stop"): "stop_cover",
        ("cover", "set"): "set_cover_position",
        ("light", "set"): "turn_on",
        # lock.open exists; the generic mapping's turn_on does not.
        ("lock", "open"): "open",
        ("climate", "set"): "set_temperature",
        ("media_player", "play"): "media_play",
        ("media_player", "pause"): "media_pause",
        ("media_player", "stop"): "media_stop",
        ("media_player", "next"): "media_next_track",
        ("media_player", "previous"): "media_previous_track",
        ("media_player", "set"): "volume_set",
        ("alarm_control_panel", "arm_home"): "alarm_arm_home",
        ("alarm_control_panel", "arm_away"): "alarm_arm_away",
        ("alarm_control_panel", "arm_night"): "alarm_arm_night",
        ("alarm_control_panel", "disarm"): "alarm_disarm",
    }

    _GENERIC_SERVICE_MAP: ClassVar[dict[str, str]] = {
        "on": "turn_on",
        "off": "turn_off",
        "toggle": "toggle",
        "open": "turn_on",
        "close": "turn_off",
        "set": "set_temperature",
    }

    # Silent allowlist: _add_domain_params copies only these keys into the
    # service call and drops everything else without an error, and a domain
    # absent from this table loses every parameter. Extend it when adding
    # support for a new parameter, or the parameter vanishes at dispatch.
    _DOMAIN_PARAMS: ClassVar[dict[str, list[str]]] = {
        "light": [
            "brightness",
            "brightness_pct",
            "color_temp_kelvin",
            "rgb_color",
            "effect",
        ],
        "climate": ["temperature", "target_temp_high", "target_temp_low", "hvac_mode"],
        "cover": ["position", "tilt_position"],
        "media_player": ["volume_level", "media_content_id", "media_content_type"],
    }

    @staticmethod
    def _normalize_light_color_temp(parameters: dict[str, Any]) -> None:
        """Convert deprecated color temp parameters to color_temp_kelvin."""
        if "color_temp_kelvin" in parameters:
            return
        if "kelvin" in parameters:
            parameters["color_temp_kelvin"] = parameters.pop("kelvin")
        elif "color_temp" in parameters:
            mired_val = parameters.pop("color_temp")
            if isinstance(mired_val, (int, float)) and mired_val > 0:
                parameters["color_temp_kelvin"] = round(1_000_000 / mired_val)

    def _add_domain_params(
        self,
        domain: str,
        parameters: dict[str, Any],
        service_data: dict[str, Any],
    ) -> None:
        if domain == "light":
            self._normalize_light_color_temp(parameters)

        allowed = self._DOMAIN_PARAMS.get(domain, [])
        for param in allowed:
            if param in parameters:
                service_data[param] = parameters[param]

    def _build_service_call(
        self,
        entity_id: str,
        domain: str,
        action: str,
        parameters: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build Home Assistant service call from action and parameters."""
        service_name, parameters = self._resolve_service_name(
            domain, action, parameters
        )

        service_data: dict[str, Any] = {"entity_id": entity_id}

        if parameters:
            self._add_domain_params(domain, parameters, service_data)

        # Remove None values
        service_data = {k: v for k, v in service_data.items() if v is not None}

        return {"domain": domain, "service": service_name, "data": service_data}

    def _predict_state_from_action(
        self,
        action: str,
        current_state: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        expected: dict[str, Any] = {}
        if action == "on":
            expected["state"] = "on"
        elif action == "off":
            expected["state"] = "off"
        elif action == "toggle":
            if current_state:
                current = current_state.get("state", "off")
                expected["state"] = "off" if current == "on" else "on"
            else:
                return None
        elif action == "open":
            expected["state"] = "open"
        elif action == "close":
            expected["state"] = "closed"
        return expected

    def _predict_attributes_from_params(
        self,
        domain: str,
        action: str,
        parameters: dict[str, Any],
        expected: dict[str, Any],
    ) -> None:
        if domain == "light" and action in ["on", "set"]:
            if "brightness" in parameters:
                expected["brightness"] = parameters["brightness"]
            if "color_temp_kelvin" in parameters:
                expected["color_temp_kelvin"] = parameters["color_temp_kelvin"]

        elif domain == "climate" and action in ["set", "heat", "cool", "auto"]:
            if "temperature" in parameters:
                expected["temperature"] = parameters["temperature"]
            if "hvac_mode" in parameters:
                expected["hvac_mode"] = parameters["hvac_mode"]
            elif action in ["heat", "cool", "auto"]:
                expected["hvac_mode"] = action

    def _predict_expected_state(
        self,
        current_state: dict[str, Any] | None,
        action: str,
        parameters: dict[str, Any] | None,
        domain: str,
    ) -> dict[str, Any] | None:
        """Predict expected entity state after operation."""
        expected = self._predict_state_from_action(action, current_state)
        if expected is None:
            return None

        if parameters:
            self._predict_attributes_from_params(domain, action, parameters, expected)

        return expected if expected else None


def entity_domain(entity_id: str, action: str) -> str:
    if "." not in entity_id:
        raise_failure(
            create_error_response(
                ErrorCode.ENTITY_INVALID_ID,
                f"Invalid entity ID format: {entity_id}",
                suggestions=[
                    "Entity ID must be in format 'domain.entity_name'",
                    "Use ha_search to find the correct entity ID",
                ],
                context={"entity_id": entity_id, "action": action},
            )
        )
    return entity_id.split(".", maxsplit=1)[0]


def validate_device_action(entity_id: str, domain: str, action: str) -> None:
    handler = get_domain_handler(domain)
    valid_actions = handler.get("valid_actions", ["on", "off", "toggle"])
    if action not in valid_actions:
        raise_failure(
            create_error_response(
                ErrorCode.SERVICE_INVALID_ACTION,
                f"Invalid action '{action}' for domain '{domain}'",
                suggestions=[
                    f"Valid actions for {domain}: {', '.join(valid_actions)}",
                    "Use 'toggle' for simple on/off control",
                ],
                context={
                    "entity_id": entity_id,
                    "action": action,
                    "valid_actions": valid_actions,
                },
            )
        )
