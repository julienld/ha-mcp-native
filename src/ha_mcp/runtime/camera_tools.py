"""Canonical camera arguments, validation and transport-neutral snapshot value."""

from dataclasses import dataclass, field
from typing import Annotated, Protocol

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


@dataclass(frozen=True, slots=True)
class CameraSnapshot:
    info: str
    data: bytes = field(repr=False)
    image_format: str


class Cameras(Protocol):
    async def snapshot(
        self, entity_id: str, width: int | None = None, height: int | None = None
    ) -> CameraSnapshot: ...


class CameraTools:
    def __init__(self, cameras: Cameras) -> None:
        self.cameras = cameras

    async def ha_get_camera_image(
        self,
        entity_id: Annotated[
            str, Field(description="Camera entity ID (e.g., 'camera.front_door')")
        ],
        width: Annotated[
            int | None,
            Field(
                description="Width for the resized image. Home Assistant "
                "rescales only when both width and height are given and "
                "the camera serves JPEG."
            ),
        ] = None,
        height: Annotated[
            int | None,
            Field(
                description="Height for the resized image. Home Assistant "
                "rescales only when both width and height are given and "
                "the camera serves JPEG."
            ),
        ] = None,
    ) -> CameraSnapshot:
        """Get a snapshot image from a Home Assistant camera entity.

        Fetches the current camera image and returns it directly for
        visual analysis (security checks, delivery verification,
        confirming a garage door actually closed), in the format Home
        Assistant serves (JPEG, PNG, or GIF), alongside a short text
        block stating the served snapshot's size and retrieval time in
        Home Assistant local time.

        Do not use it to inspect the camera's state or attributes — use
        ha_search or ha_get_state for those; it returns an image, not
        state. Use it when you need to see the current scene, and on
        high-resolution cameras pass width and height to reduce token
        usage.

        EXAMPLE: ha_get_camera_image(entity_id="camera.backyard", width=640, height=480)
        """
        if not entity_id or "." not in entity_id:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid entity_id format: {entity_id}. "
                    "Expected format: camera.entity_name",
                )
            )

        domain = entity_id.split(".", maxsplit=1)[0]
        if domain != "camera":
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Entity {entity_id} is not a camera entity. "
                    f"Domain is '{domain}', expected 'camera'.",
                    context={"entity_id": entity_id},
                )
            )

        return await self.cameras.snapshot(entity_id, width, height)
