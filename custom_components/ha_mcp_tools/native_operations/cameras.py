"""Read a pinned Core camera owner without a loopback proxy or access token."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.errors import create_entity_not_found_error
from ha_mcp.image_info import resolve_image_info
from ha_mcp.runtime.camera_tools import CameraSnapshot
from ha_mcp.runtime.camera_values import _detect_image_format, _snapshot_info_text
from ha_mcp.runtime.projection import resolve_local_timezone
from ha_mcp.tool_api import MAX_TOOL_IMAGE_BYTES, CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from .registry_access import Authorize, authorize_actor, registry_failure


@dataclass(slots=True)
class NativeCameras:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)

    def _owner(self, entity_id: str) -> Any:
        from homeassistant.components.camera import get_camera_from_entity_id

        try:
            owner = get_camera_from_entity_id(self.hass, entity_id)
        except Exception as error:
            raise ToolFailure(
                "ENTITY_NOT_FOUND",
                "Camera not found",
                data=create_entity_not_found_error(entity_id),
                status="not_started",
            ) from error
        if not owner.is_on:
            raise ToolFailure(
                "SERVICE_CALL_FAILED", "The camera is off", status="not_started"
            )
        return owner

    async def _read(
        self, entity_id: str, owner: Any, width: int | None, height: int | None
    ) -> Any:
        from homeassistant.components.camera import (
            CAMERA_IMAGE_TIMEOUT,
            async_get_image,
        )

        await authorize_actor(self.actor, self.authorize)
        if self._owner(entity_id) is not owner:
            raise ToolFailure(
                "CONFIG_CONFLICT", "The camera owner changed", status="not_started"
            )
        # Core's public function resolves the owner synchronously before its
        # first suspension. Keep this await in the same admitted worker.
        return await async_get_image(
            self.hass, entity_id, CAMERA_IMAGE_TIMEOUT, width, height
        )

    async def snapshot(
        self, entity_id: str, width: int | None = None, height: int | None = None
    ) -> CameraSnapshot:
        try:
            await authorize_actor(self.actor, self.authorize)
            owner = self._owner(entity_id)
            image = await await_owned_io(self._read(entity_id, owner, width, height))
            retrieved = datetime.now(UTC)
            await authorize_actor(self.actor, self.authorize)
            if self._owner(entity_id) is not owner:
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The camera changed while its image was being read",
                    status="not_started",
                )
            if not image.content or len(image.content) > MAX_TOOL_IMAGE_BYTES:
                raise ToolFailure(
                    "IMAGE_SIZE_UNSUPPORTED",
                    "Core returned an empty image or one larger than the supported 20 MiB limit",
                    status="not_started",
                )
            content, content_type = image.content, image.content_type
            timezone = self.hass.config.time_zone

            def describe() -> CameraSnapshot:
                image_format, image_size = resolve_image_info(
                    content, _detect_image_format(content_type)
                )
                local, resolved = resolve_local_timezone(timezone)
                return CameraSnapshot(
                    _snapshot_info_text(
                        image_format,
                        image_size,
                        retrieved.astimezone(local),
                        resolved != timezone,
                    ),
                    content,
                    image_format,
                )

            result: CameraSnapshot = await await_owned_io(
                self.hass.async_add_executor_job(describe)
            )
            await authorize_actor(self.actor, self.authorize)
            if self._owner(entity_id) is not owner:
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The camera changed while its image was being described",
                    status="not_started",
                )
            return result
        except Exception as error:
            raise registry_failure(error, {"entity_id": entity_id}) from error
