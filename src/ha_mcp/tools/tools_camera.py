"""
Camera tools for Home Assistant MCP server.

This module provides camera-related tools including snapshot retrieval
that returns images directly to the LLM for visual analysis, alongside a
short text block stating the served snapshot's size and retrieval time.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp._vendor.fastmcp.utilities.types import Image
from ha_mcp.backends.legacy import legacy_errors
from ha_mcp.errors import (
    ErrorCode,
    create_auth_error,
    create_entity_not_found_error,
    create_error_response,
)
from ha_mcp.image_info import resolve_image_info
from ha_mcp.runtime.camera_tools import CameraSnapshot
from ha_mcp.runtime.camera_tools import CameraTools as _CameraContractTools
from ha_mcp.runtime.camera_values import _detect_image_format as _detect_image_format
from ha_mcp.runtime.camera_values import _snapshot_info_text as _snapshot_info_text

from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .response_helpers import fetch_ha_timezone, resolve_local_timezone
from .tool_hints import read_only_hints

logger = logging.getLogger(__name__)


class RemoteCameras:
    """Camera snapshot retrieval tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _check_response(response: Any, entity_id: str) -> None:
        """Validate a camera-proxy HTTP response, raising a structured ToolError.

        401 maps to an authentication error, 404 to a missing-entity error,
        any other 4xx/5xx to a service-call failure, and an empty 2xx body
        is a service failure too (HA answered but had no image to serve).
        """
        if response.status_code == 401:
            raise_tool_error(
                create_auth_error(
                    "Invalid authentication token for camera access",
                    context={"entity_id": entity_id},
                )
            )
        if response.status_code == 404:
            raise_tool_error(
                create_entity_not_found_error(
                    entity_id,
                    details="Use ha_search to find available cameras",
                )
            )
        if response.status_code >= 400:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to retrieve camera image: HTTP {response.status_code}",
                    context={"entity_id": entity_id},
                )
            )
        if not response.content:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Camera {entity_id} returned empty image data. The camera "
                    "may be offline or temporarily unavailable.",
                    context={"entity_id": entity_id},
                )
            )

    async def snapshot(
        self, entity_id: str, width: int | None = None, height: int | None = None
    ) -> CameraSnapshot:
        # Build the camera proxy URL with optional size parameters
        # Home Assistant camera proxy API: /api/camera_proxy/<entity_id>
        endpoint = f"/camera_proxy/{entity_id}"

        params = {}
        if width is not None:
            params["width"] = str(width)
        if height is not None:
            params["height"] = str(height)

        try:
            response = await self._client.httpx_client.get(
                endpoint, params=params or None
            )
            self._check_response(response, entity_id)
            # Sample the clock the moment HA handed us the bytes; the zone
            # is resolved separately so a slow timezone lookup only delays
            # the label, never the timestamp.
            retrieved_at = datetime.now(UTC)

            content_type = response.headers.get("content-type", "image/jpeg")
            # Cameras can mislabel their payload, so resolve the format
            # from the bytes: the reported label, size, and Image block
            # must all describe what was actually served.
            image_format, image_size = resolve_image_info(
                response.content, _detect_image_format(content_type)
            )
            ha_timezone, fetch_failed = await fetch_ha_timezone(self._client)
            local_tz, resolved_timezone = resolve_local_timezone(ha_timezone)
            # A failed fetch, or a zone name tzdata cannot resolve, both
            # fall back to UTC — the note keeps that distinct from a
            # genuine UTC install.
            timezone_fallback = fetch_failed or resolved_timezone != ha_timezone

            logger.info(
                f"Retrieved camera image from {entity_id} "
                f"({len(response.content)} bytes, format={image_format})"
            )

            # Return the info text plus a FastMCP Image object (auto-converted
            # to MCP TextContent and ImageContent, in this order)
            return CameraSnapshot(
                _snapshot_info_text(
                    image_format,
                    image_size,
                    retrieved_at.astimezone(local_tz),
                    timezone_fallback,
                ),
                response.content,
                image_format,
            )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error retrieving camera image from {entity_id}: {e}")
            # The default raise_error=True path raises inside the helper and
            # never returns; the surrounding raise keeps that termination
            # explicit so no path through this function falls off the end.
            raise exception_to_structured_error(
                e,
                context={"entity_id": entity_id},
                suggestions=["Ensure the camera is online and accessible"],
            ) from e



def _as_fastmcp_image(
    function: Callable[..., Awaitable[CameraSnapshot]],
) -> Callable[..., Awaitable[tuple[str, Image]]]:
    @wraps(function)
    async def invoke(*args: Any, **kwargs: Any) -> tuple[str, Image]:
        snapshot = await function(*args, **kwargs)
        return snapshot.info, Image(data=snapshot.data, format=snapshot.image_format)

    return invoke


class CameraTools(RemoteCameras):
    @property
    def cameras(self) -> RemoteCameras:
        return self

    ha_get_camera_image = tool(
        name="ha_get_camera_image",
        tags={"Camera"},
        annotations=read_only_hints("Get Camera Image", open_world=False),
        output_schema=None,
    )(
        log_tool_usage(
            legacy_errors(_as_fastmcp_image(_CameraContractTools.ha_get_camera_image))
        )
    )


def register_camera_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant camera tools."""
    register_tool_methods(mcp, CameraTools(client))
