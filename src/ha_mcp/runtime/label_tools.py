"""One public label contract, bound to a native or legacy domain service."""

from typing import Annotated, Any, Protocol

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class Labels(Protocol):
    async def get(self, label_id: str | None = None) -> dict[str, Any]: ...
    async def set(
        self,
        name: str,
        label_id: str | None = None,
        color: str | None = None,
        icon: str | None = None,
        description: str | None = None,
        areas: str | list[str] | None = None,
    ) -> dict[str, Any]: ...
    async def remove(self, label_id: str) -> dict[str, Any]: ...


class LabelTools:
    def __init__(self, labels: Labels) -> None:
        self.labels = labels

    async def ha_config_get_label(
        self,
        label_id: Annotated[
            str | None,
            Field(
                description="ID of the label to retrieve.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get label info - list all labels or get a specific one by ID.

        Without a label_id: Lists all Home Assistant labels with their configurations.
        With a label_id: Returns configuration for that specific label.

        EXAMPLES:
        - List all labels: ha_config_get_label()
        - Get specific label: ha_config_get_label("my_label_id")

        Use ha_config_set_label() to create or update labels.
        Use ha_set_entity(labels=["label1", "label2"]) to assign labels to entities,
        ha_set_device(labels=[...]) for devices, or
        ha_set_area_or_floor(kind="area", labels=[...]) for areas.
        """
        return await self.labels.get(label_id=label_id)

    async def ha_config_set_label(
        self,
        name: Annotated[str, Field(description="Display name for the label")],
        label_id: Annotated[
            str | None,
            Field(
                description="Label ID for updates.",
                default=None,
            ),
        ] = None,
        color: Annotated[
            str | None,
            Field(
                description="Color for the label (e.g., 'red', 'blue', 'green', or hex like '#FF5733')",
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
        description: Annotated[
            str | None,
            Field(
                description="Description of the label's purpose",
                default=None,
            ),
        ] = None,
        areas: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Area IDs to apply this label to (adds the label without "
                    "removing existing ones). Omit to leave area assignments "
                    "unchanged; an empty list is a no-op (assigns nothing and "
                    "removes nothing). To clear an area's labels use "
                    "ha_set_area_or_floor(kind='area', labels=[])."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create or update a Home Assistant label.

        Creates a new label if label_id is not provided, or updates an existing
        label if label_id is provided. Labels are a cross-domain tagging system
        for entities, devices, and areas.

        EXAMPLES:
        - Create: ha_config_set_label("Security", color="red", icon="mdi:shield", description="Security-related devices")
        - Update: ha_config_set_label("Updated Name", label_id="my_label_id", color="blue")
        - Create and apply to areas: ha_config_set_label("Site Home", areas=["kitchen", "living_room"])

        After creating a label, use ha_set_entity(labels=["label_id"]) to assign it to entities,
        ha_set_device(labels=["label_id"]) for devices, or
        ha_set_area_or_floor(kind="area", labels=["label_id"]) for areas (replaces the area's set).
        """
        return await self.labels.set(
            name=name,
            label_id=label_id,
            color=color,
            icon=icon,
            description=description,
            areas=areas,
        )

    async def ha_config_remove_label(
        self,
        label_id: Annotated[
            str,
            Field(description="ID of the label to delete"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant label.

        Removes the label from the label registry. This will also remove the label
        from all entities, devices, and areas that have it assigned.

        EXAMPLES:
        - Delete label: ha_config_remove_label("my_label_id")

        Use ha_config_get_label() to find label IDs.

        **WARNING:** Deleting a label will remove it from all assigned entities.
        This action cannot be undone.
        """
        return await self.labels.remove(label_id=label_id)
