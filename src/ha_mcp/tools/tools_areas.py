"""
Area and floor management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant areas and floors - essential organizational features for smart homes.
"""

import asyncio
import logging
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import LegacyBackendTools, legacy_errors, legacy_errors_sync
from ..backends.legacy_config_errors import raise_legacy_classified
from ..errors import ErrorCode, create_error_response
from ..runtime.area_writes import AreaWriteTools
from ..runtime.registries import AreaDiscoveryTools
from .auto_backup import with_auto_backup
from .component_registries import fetch_registries_via_component
from .config_helpers.registry import validate_registry_ids
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)


class AreaTools(LegacyBackendTools, AreaWriteTools):
    """Area and floor management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        super().__init__(client)

    ha_list_floors_areas = tool(
        name="ha_list_floors_areas",
        tags={"Areas & Floors"},
        annotations=read_only_hints("List Floors and Areas", open_world=False),
    )(log_tool_usage(legacy_errors(AreaDiscoveryTools.ha_list_floors_areas)))

    _map_area_write_error = staticmethod(raise_legacy_classified)

    async def _validate_area_refs(
        self, labels: list[str] | None, floor_id: str | None
    ) -> None:
        await validate_registry_ids(
            self._client, None, labels, None, floor_id=floor_id, fail_closed=True
        )

    async def _area_floor_change(
        self, kind: str, action: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        result: dict[str, Any] = await self._client.send_websocket_message(
            {"type": f"config/{kind}_registry/{action}", **data}
        )
        return result

    async def _list_area_rows(self) -> dict[str, Any]:
        result: dict[str, Any] = await self._client.send_websocket_message(
            {"type": "config/area_registry/list"}
        )
        return result

    def _raise_list_error(self, error: Exception, progress: dict[str, Any]) -> NoReturn:
        if isinstance(error, ToolError):
            raise error
        exception_to_structured_error(
            error,
            context=progress,
            suggestions=[
                "Check Home Assistant connection",
                "Verify WebSocket connection is active",
            ],
        )

    async def _fetch_area_floor_registries(
        self, progress: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return ``(areas, floors)`` as one consistent snapshot when possible.

        Prefers the ``ha_mcp_tools`` component's ``registries`` capability — a
        single in-process read of both registries, closing the TOCTOU window
        between two independent list calls. Falls back to the legacy 2-call
        ``asyncio.gather`` when the component is unavailable, downgraded, or
        errors on this frame. Updates ``progress["phase"]`` for the caller's
        error-context reporting either way.
        """
        component_result = await fetch_registries_via_component(
            self._client, ["area", "floor"]
        )
        if component_result is not None:
            progress["phase"] = "registries_fetched"
            return (
                component_result.get("areas") or [],
                component_result.get("floors") or [],
            )

        # Fetch both registries concurrently. Sequential awaits add a
        # round-trip per call on the WS transport; gather halves the
        # tool-side latency. Use return_exceptions=True so a failure on
        # one side doesn't cancel the other — the post-fetch guard
        # below reports both registries' state in the error context for
        # diagnosis. Indexed access + explicit annotations rather than
        # tuple-unpack — gather returns list[Any] which mypy can't
        # statically narrow to a 2-tuple.
        results = await asyncio.gather(
            self._client.send_websocket_message({"type": "config/area_registry/list"}),
            self._client.send_websocket_message({"type": "config/floor_registry/list"}),
            return_exceptions=True,
        )
        progress["phase"] = "registries_fetched"

        # Re-raise transport-level exceptions from either fetch so the
        # outer except handler classifies them via exception_to_structured_error.
        if isinstance(results[0], BaseException):
            raise results[0]
        if isinstance(results[1], BaseException):
            raise results[1]
        areas_result: dict[str, Any] = results[0]
        floors_result: dict[str, Any] = results[1]

        # A response with success=True but no "result" key is malformed —
        # treat it as a service call failure rather than silently returning
        # floor_count=0, area_count=0 on a populated instance.
        areas_ok = areas_result.get("success") and "result" in areas_result
        floors_ok = floors_result.get("success") and "result" in floors_result
        if not (areas_ok and floors_ok):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Failed to retrieve area or floor registry",
                    context={
                        "areas_success": areas_result.get("success"),
                        "floors_success": floors_result.get("success"),
                        "areas_response_keys": sorted(areas_result.keys()),
                        "floors_response_keys": sorted(floors_result.keys()),
                    },
                    suggestions=[
                        "Check Home Assistant connection",
                        "Verify WebSocket connection is active",
                    ],
                )
            )

        return areas_result["result"], floors_result["result"]

    _build_area_or_floor_message = legacy_errors_sync(
        AreaWriteTools._build_area_or_floor_message
    )

    _label_verify_context = staticmethod(AreaWriteTools._label_verify_context)

    _verify_area_labels_written = legacy_errors(
        AreaWriteTools._verify_area_labels_written, classify=False
    )

    _area_or_floor_write_success = legacy_errors(
        AreaWriteTools._area_or_floor_write_success, classify=False
    )

    # ============================================================
    # COMBINED SET / REMOVE
    # ============================================================

    ha_set_area_or_floor = tool(
        name="ha_set_area_or_floor",
        tags={"Areas & Floors"},
        annotations=write_hints(
            "Create or Update Area or Floor",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(
        with_auto_backup(
            domain="area_or_floor",
            id_fn=lambda kw: (
                f"{kw['kind']}:{kw['id']}" if kw.get("kind") and kw.get("id") else ""
            ),
        )(log_tool_usage(legacy_errors(AreaWriteTools.ha_set_area_or_floor)))
    )

    ha_remove_area_or_floor = tool(
        name="ha_remove_area_or_floor",
        tags={"Areas & Floors"},
        annotations=write_hints(
            "Remove Area or Floor", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(
            domain="area_or_floor",
            id_fn=lambda kw: (
                f"{kw['kind']}:{kw['id']}" if kw.get("kind") and kw.get("id") else ""
            ),
        )(log_tool_usage(legacy_errors(AreaWriteTools.ha_remove_area_or_floor)))
    )


def register_area_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant area and floor management tools."""
    register_tool_methods(mcp, AreaTools(client))


__all__ = [
    "AreaTools",
    "register_area_tools",
]
