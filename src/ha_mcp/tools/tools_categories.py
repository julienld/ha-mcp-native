"""
Category management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant categories. Categories are domain-scoped organizational groups
(e.g., for automations, scripts, scenes, helpers) introduced in Home Assistant 2024.4.

To assign categories to entities, use ha_set_entity(categories=...).
"""

import logging
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..backends.legacy_config_errors import raise_legacy_classified
from ..errors import ErrorCode, create_error_response
from ..runtime.category_writes import CategoryWriteTools
from ..runtime.labels import CategoryDiscoveryTools
from .auto_backup import with_auto_backup
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)


class CategoryTools(CategoryWriteTools):
    """Category management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _category_change(
        self, action: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        result: dict[str, Any] = await self._client.send_websocket_message(
            {"type": f"config/category_registry/{action}", **data}
        )
        return result

    _map_category_write_error = staticmethod(raise_legacy_classified)

    def _raise_read_error(self, error: Exception, context: dict[str, Any]) -> NoReturn:
        if isinstance(error, ToolError):
            raise error
        suggestions = [
            "Check Home Assistant connection",
            "Verify WebSocket connection is active",
        ]
        suggestions.append(
            "Ensure scope is valid (e.g., 'automation', 'script', 'scene', 'helpers')"
        )
        exception_to_structured_error(error, context=context, suggestions=suggestions)

    async def _list_categories(
        self, scope: str, category_id: str | None
    ) -> list[dict[str, Any]]:
        result = await self._client.send_websocket_message(
            {"type": "config/category_registry/list", "scope": scope}
        )
        if not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    result.get("error", "Failed to get categories"),
                    context={"scope": scope, "category_id": category_id},
                )
            )
        categories: list[dict[str, Any]] = result.get("result", [])
        return categories

    ha_config_get_category = tool(
        name="ha_config_get_category",
        tags={"Labels & Categories"},
        annotations=read_only_hints("Get Category", open_world=False),
    )(log_tool_usage(legacy_errors(CategoryDiscoveryTools.ha_config_get_category)))

    ha_config_set_category = tool(
        name="ha_config_set_category",
        tags={"Labels & Categories"},
        annotations=write_hints(
            "Create or Update Category",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(
        with_auto_backup(
            domain="category",
            id_fn=lambda kw: (
                f"{kw['scope']}:{kw['category_id']}"
                if kw.get("scope") and kw.get("category_id")
                else ""
            ),
        )(log_tool_usage(legacy_errors(CategoryWriteTools.ha_config_set_category)))
    )

    ha_config_remove_category = tool(
        name="ha_config_remove_category",
        tags={"Labels & Categories"},
        annotations=write_hints(
            "Remove Category", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(
            domain="category",
            id_fn=lambda kw: (
                f"{kw['scope']}:{kw['category_id']}"
                if kw.get("scope") and kw.get("category_id")
                else ""
            ),
        )(log_tool_usage(legacy_errors(CategoryWriteTools.ha_config_remove_category)))
    )


def register_category_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant category management tools."""
    register_tool_methods(mcp, CategoryTools(client))
