"""A real Camera entity serving a local PNG without HTTP or network credentials."""

import base64
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

UNIQUE_ID = "native-migration-camera"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5mUAAAAASUVORK5CYII="
)


class NativeFixtureCamera(Camera):
    _attr_name = "Native migration camera"
    _attr_unique_id = UNIQUE_ID

    def __init__(self) -> None:
        super().__init__()
        self.content_type = "image/png"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes:
        calls = self.hass.data["native_fixture"].setdefault("camera_calls", [])
        calls.append({"width": width, "height": height})
        del calls[:-8]
        return PNG


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: Any = None,
) -> None:
    async_add_entities([NativeFixtureCamera()])


def describe_camera(hass: HomeAssistant) -> dict[str, Any]:
    entity_id = er.async_get(hass).async_get_entity_id(
        "camera", "native_fixture", UNIQUE_ID
    )
    return {
        "entity_id": entity_id,
        "ready": entity_id is not None and hass.states.get(entity_id) is not None,
        "calls": hass.data["native_fixture"].get("camera_calls", []),
    }
