"""Core-owned Z-Wave configuration values, without creating config entities."""

from typing import Any

from ha_mcp.runtime.radio_parameters import _no_raw_value, read

from .radio_access import RadioSession
from .registry_access import registry_json


class NativeParameters:
    def __init__(self, session: RadioSession, node: Any) -> None:
        self.session = session
        self.node = node

    async def parameters(self, device_id: str) -> Any:
        self.session.validate(
            "zwave_js/get_config_parameters", {"device_id": device_id}
        )
        self.session.verify()
        # Core 2026.10.2 exposes this projection only in its WS handler. Values
        # and enum identities come from the loaded node/metadata objects.
        result = {}
        for identifier, value in self.node.get_configuration_values().items():
            metadata = value.metadata
            description = {
                key: getattr(metadata, key)
                for key in (
                    "description",
                    "label",
                    "type",
                    "min",
                    "max",
                    "unit",
                    "writeable",
                    "readable",
                    "default",
                )
            }
            if metadata.states:
                description["states"] = metadata.states
            result[identifier] = {
                "property": value.property_,
                "property_key": value.property_key,
                "endpoint": value.endpoint,
                "configuration_value_type": value.configuration_value_type.value,
                "metadata": description,
                "value": value.value,
            }
        return registry_json(result)

    async def raw(self, device_id: str, property_: int) -> Any:
        payload = self.session.validate(
            "zwave_js/get_raw_config_parameter",
            {"device_id": device_id, "property": property_},
        )
        value = await self.session.read(
            lambda: self.node.async_get_raw_config_parameter_value(payload["property"])
        )
        if value is None:
            _no_raw_value(
                {
                    "device_id": device_id,
                    "property": property_,
                    "refresh_requested": True,
                }
            )
        return value


async def parameters(
    session: RadioSession, node: Any, action: str, args: dict[str, Any]
) -> dict[str, Any]:
    return await read(NativeParameters(session, node), action, args)
