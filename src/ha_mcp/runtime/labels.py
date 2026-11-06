"""Shared label/category read contracts, backed by registry semantic operations."""

from __future__ import annotations

import logging
from functools import wraps
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import BackendTools, ToolFailure

from .errors import raise_failure
from .label_tools import LabelTools
from .label_values import label_get_result
from .validation import validate_identifier_not_empty

logger = logging.getLogger(__name__)


class LabelDiscoveryTools(BackendTools):
    def _raise_read_error(self, error: Exception, context: dict[str, Any]) -> NoReturn:
        raise_failure(
            create_error_response(ErrorCode.INTERNAL_ERROR, str(error), context=context)
        )

    async def _list_labels(
        self, context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        result = await self.backend.execute(
            "registries.snapshot", {"registries": ["label"]}, self.context
        )
        labels: list[dict[str, Any]] = result["labels"]
        return labels

    @wraps(LabelTools.ha_config_get_label)
    async def ha_config_get_label(self, label_id: Any = None) -> dict[str, Any]:
        try:
            if label_id is not None:
                validate_identifier_not_empty(
                    label_id,
                    "label_id",
                    suggestions=[
                        "Omit label_id entirely to list all labels",
                        "Pass a valid non-empty label_id",
                    ],
                    context={"action": "get"},
                )
            return label_get_result(
                await self._list_labels(context={"label_id": label_id}), label_id
            )
        except ToolFailure:
            raise
        except Exception as error:  # noqa: BLE001 - preserve first-increment bridge errors
            self._raise_read_error(error, {"label_id": label_id})


class CategoryDiscoveryTools(BackendTools):
    def _raise_read_error(self, error: Exception, context: dict[str, Any]) -> NoReturn:
        raise_failure(
            create_error_response(ErrorCode.INTERNAL_ERROR, str(error), context=context)
        )

    async def _list_categories(
        self, scope: str, category_id: str | None
    ) -> list[dict[str, Any]]:
        result = await self.backend.execute(
            "registries.snapshot",
            {"registries": ["category"], "category_scopes": [scope]},
            self.context,
        )
        categories: list[dict[str, Any]] = result["categories"][scope]
        return categories

    async def ha_config_get_category(
        self,
        scope: Annotated[
            str,
            Field(
                description="Domain scope for categories (e.g., 'automation', 'script', 'scene', 'helpers').",
            ),
        ],
        category_id: Annotated[
            str | None,
            Field(
                description="ID of the category to retrieve.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get category info - list all categories for a scope or get a specific one by ID.

        Without a category_id: Lists all Home Assistant categories for the given scope.
        With a category_id: Returns configuration for that specific category.

        Categories are domain-scoped organizational groups for automations, scripts, scenes, and helpers.

        EXAMPLES:
        - List helper categories: ha_config_get_category("helpers")
        - Get specific category: ha_config_get_category("automation", category_id="my_category_id")

        Use ha_config_set_category() to create or update categories.
        Use ha_set_entity(categories={"automation": "category_id"}) to assign categories to entities.
        """
        try:
            # ``None`` stays the documented "list-all" sentinel; explicit
            # empty/whitespace is rejected by ``validate_identifier_not_empty``.
            if category_id is not None:
                validate_identifier_not_empty(
                    category_id,
                    "category_id",
                    suggestions=[
                        "Omit category_id to list all categories for the scope",
                        "Pass a valid non-empty category_id",
                    ],
                    context={"action": "get", "scope": scope},
                )
            categories = await self._list_categories(scope, category_id)

            if category_id is None:
                return {
                    "success": True,
                    "count": len(categories),
                    "categories": categories,
                    "scope": scope,
                    "message": f"Found {len(categories)} category(ies) for scope '{scope}'",
                }

            category = next(
                (cat for cat in categories if cat.get("category_id") == category_id),
                None,
            )

            if category:
                return {
                    "success": True,
                    "category_id": category_id,
                    "category": category,
                    "scope": scope,
                    "message": f"Found category: {category.get('name', category_id)}",
                }
            else:
                available_ids = [cat.get("category_id") for cat in categories[:10]]
                raise_failure(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Category not found: {category_id}",
                        context={
                            "category_id": category_id,
                            "scope": scope,
                            "available_category_ids": available_ids,
                        },
                        suggestions=[
                            f"Use ha_config_get_category('{scope}') without category_id to see all categories"
                        ],
                    )
                )

        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error getting categories: {e}")
            self._raise_read_error(e, {"scope": scope, "category_id": category_id})
