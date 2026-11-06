"""Category authoring shares orchestration across remote and Core owners."""

from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import ToolFailure

from .errors import raise_classified, raise_failure
from .labels import CategoryDiscoveryTools
from .registry_write_context import RegistryWriteContext
from .validation import validate_identifier_not_empty


class CategoryWriteTools(CategoryDiscoveryTools, RegistryWriteContext):
    def _map_category_write_error(
        self, error: Exception, context: dict[str, Any], suggestions: list[str]
    ) -> NoReturn:
        raise_classified(error, context, suggestions=suggestions)

    async def _category_change(
        self, action: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._registry_change(
            "category",
            action,
            data,
            "ha_config_remove_category"
            if action == "delete"
            else "ha_config_set_category",
        )

    async def ha_config_set_category(
        self,
        name: Annotated[str, Field(description="Display name for the category")],
        scope: Annotated[
            str,
            Field(
                description="Domain scope for the category (e.g., 'automation', 'script', 'scene', 'helpers').",
            ),
        ],
        category_id: Annotated[
            str | None,
            Field(
                description="Category ID for updates.",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:tag', 'mdi:label')",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant category.

        Creates a new category if category_id is not provided, or updates an existing category if category_id is provided.

        Categories are domain-scoped organizational groups for automations, scripts,
        scenes, and helpers. Unlike labels (which are cross-domain), categories are
        specific to a single domain scope.

        EXAMPLES:
        - Create automation category: ha_config_set_category("Lighting", scope="automation")
        - Create with icon: ha_config_set_category("Security", scope="automation", icon="mdi:shield")
        - Update category: ha_config_set_category("Updated Name", scope="automation", category_id="my_category_id")

        After creating a category, use ha_set_entity(categories={"automation": "category_id"}) to assign it.
        """
        try:
            # ``None`` stays the documented "create-new" sentinel; explicit
            # empty/whitespace is rejected so the create/update discriminator
            # below cannot silently route an intended update to create.
            if category_id is not None:
                validate_identifier_not_empty(
                    category_id,
                    "category_id",
                    suggestions=[
                        "Omit category_id entirely to create a new category",
                        "Pass a valid category_id to update an existing category",
                    ],
                    context={"action": "set", "scope": scope, "name": name},
                )
            action = "update" if category_id else "create"

            message: dict[str, Any] = {
                "scope": scope,
                "name": name,
            }

            if action == "update":
                message["category_id"] = category_id

            if icon is not None:
                message["icon"] = icon

            result = await self._category_change(action, message)

            if result.get("success"):
                category_data = result.get("result", {})
                action_past = "created" if action == "create" else "updated"
                return {
                    "success": True,
                    **(
                        {"warnings": result["warnings"]}
                        if result.get("warnings")
                        else {}
                    ),
                    "category_id": category_data.get("category_id"),
                    "category_data": category_data,
                    "scope": scope,
                    "message": f"Successfully {action_past} category: {name}",
                }
            else:
                error_str = str(result.get("error", "")).lower()
                if "not found" in error_str or "doesn't exist" in error_str:
                    return raise_failure(
                        create_error_response(
                            ErrorCode.RESOURCE_NOT_FOUND,
                            f"Category not found: {category_id}",
                            context={
                                "name": name,
                                "scope": scope,
                                "category_id": category_id,
                            },
                            suggestions=[
                                f"Use ha_config_get_category('{scope}') without category_id to see all categories",
                            ],
                        )
                    )
                return raise_failure(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to {action} category: {result.get('error', 'Unknown error')}",
                        context={
                            "name": name,
                            "scope": scope,
                            "category_id": category_id,
                        },
                    )
                )

        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001
            return self._map_category_write_error(
                e,
                context={"name": name, "scope": scope, "category_id": category_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify the category name is valid",
                    "For updates, verify the category_id exists using ha_config_get_category()",
                ],
            )

    async def ha_config_remove_category(
        self,
        scope: Annotated[
            str,
            Field(
                description="Domain scope for the category (e.g., 'automation', 'script', 'scene', 'helpers').",
            ),
        ],
        category_id: Annotated[
            str,
            Field(description="ID of the category to delete"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant category.

        Removes the category from the category registry for the given scope.
        This will also remove the category assignment from all entities in that scope.

        EXAMPLES:
        - Delete category: ha_config_remove_category("automation", "my_category_id")

        Use ha_config_get_category() to find category IDs.

        **WARNING:** Deleting a category will remove it from all assigned entities.
        This action cannot be undone.
        """
        try:
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                category_id,
                "category_id",
                suggestions=[
                    "Pass a valid category_id (use ha_config_get_category() to list)",
                ],
                context={"action": "remove", "scope": scope},
            )
            message: dict[str, Any] = {
                "scope": scope,
                "category_id": category_id,
            }

            result = await self._category_change("delete", message)

            if result.get("success"):
                return {
                    "success": True,
                    **(
                        {"warnings": result["warnings"]}
                        if result.get("warnings")
                        else {}
                    ),
                    "category_id": category_id,
                    "scope": scope,
                    "message": f"Successfully deleted category: {category_id}",
                }
            else:
                error_str = str(result.get("error", "")).lower()
                if "not found" in error_str or "doesn't exist" in error_str:
                    return raise_failure(
                        create_error_response(
                            ErrorCode.RESOURCE_NOT_FOUND,
                            f"Category not found: {category_id}",
                            context={"category_id": category_id, "scope": scope},
                            suggestions=[
                                f"Use ha_config_get_category('{scope}') without category_id to see all categories",
                            ],
                        )
                    )
                return raise_failure(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to delete category: {result.get('error', 'Unknown error')}",
                        context={"category_id": category_id, "scope": scope},
                    )
                )

        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001
            return self._map_category_write_error(
                e,
                context={"category_id": category_id, "scope": scope},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify the category_id exists using ha_config_get_category()",
                ],
            )
