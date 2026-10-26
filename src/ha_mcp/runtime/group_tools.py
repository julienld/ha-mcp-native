"""Public service-group contracts shared with the remote compatibility adapter."""

from typing import Annotated, Any, Protocol

from pydantic import Field

from ha_mcp.tool_api import ToolFailure
from ha_mcp.tools.coercion import JSON_STRING_COERCION

from .group_values import _build_group_service_data
from .validation import validate_identifier_not_empty

_OBJECT_ID_DESCRIPTION = (
    "Group identifier: a lowercase slug without the group. prefix "
    "(e.g., living_room_lights)"
)


class Groups(Protocol):
    async def list(self, limit: int, offset: int) -> dict[str, Any]: ...

    async def set(
        self, service_data: dict[str, Any], *, wait: bool
    ) -> dict[str, Any]: ...

    async def remove(self, object_id: str, *, wait: bool) -> dict[str, Any]: ...


def _invalid_arguments(error: ToolFailure) -> ToolFailure:
    return ToolFailure(error.code, str(error), data=error.data, status="not_started")


class GroupTools:
    def __init__(self, groups: Groups) -> None:
        self.groups = groups

    async def ha_config_list_groups(
        self,
        limit: Annotated[
            int,
            Field(
                default=100,
                ge=1,
                le=500,
                description="Max groups to return per page",
            ),
        ] = 100,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of groups to skip for pagination",
            ),
        ] = 0,
    ) -> dict[str, Any]:
        """List Home Assistant entity groups with their member entities.

        Returns one page of old-style groups created via the group.set service
        or YAML configuration (platform-specific groups such as light or cover
        groups are separate entities); `total_count` and `has_more` report the
        full set. Each group carries Core's state fields and attributes, its
        members as `entity_ids`, and its object_id. `auto` is true for a group
        created by ha_config_set_group (group.set) and false for a YAML group.
        The in-process runtime also reads the all/any mode from its Core owner.

        EXAMPLE: ha_config_list_groups(offset=100)
        """

        return await self.groups.list(limit, offset)

    async def ha_config_set_group(
        self,
        object_id: Annotated[str, Field(description=_OBJECT_ID_DESCRIPTION)],
        entities: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="List of entity IDs for the group. When updating, replaces all entities;"
                " an empty list empties the group, or creates it without members.",
                default=None,
            ),
        ] = None,
        name: Annotated[
            str | None,
            Field(
                description="Friendly display name; a new group without one is named after object_id",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:lightbulb-group')",
                default=None,
            ),
        ] = None,
        all_on: Annotated[
            bool | None,
            Field(
                description="If True, all entities must be on for group to be on. Remote Core state does not"
                " expose this mode; the in-process runtime can read and restore it.",
                default=None,
            ),
        ] = None,
        add_entities: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Entities to add to the group's members",
                default=None,
            ),
        ] = None,
        remove_entities: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Entities to remove from the group's members",
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool,
            Field(
                description="Wait for group to be queryable before returning. Set to False for bulk"
                " operations.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Create or update a service-based Home Assistant entity group via the group.set service.

        **When NOT to use:** for typical "combine these entities into one controllable group"
        requests, prefer `ha_config_set_helper(helper_type="group", ...)`. Config-entry-backed
        groups are registered in the entity registry, so `ha_set_entity` can assign them to
        areas and they are deletable via `ha_remove_helpers_integrations`.

        **When to use:** compatibility with existing groups already configured via group.set
        or YAML, or the rare case where entity-registry membership is explicitly unwanted.
        Groups created here are only removable via `ha_config_remove_group` —
        `ha_remove_helpers_integrations` will not find them. They survive group.reload but
        not a restart.

        Core creates group.<object_id> when it does not exist (members from entities or
        add_entities) and updates it otherwise. entities, add_entities, and remove_entities
        are mutually exclusive.

        EXAMPLES:
        - Create: ha_config_set_group("bedroom_lights", entities=["light.lamp", "light.ceiling"], name="Bedroom Lights")
        - Add entities: ha_config_set_group("lights", add_entities=["light.extra"])
        """

        try:
            validate_identifier_not_empty(
                object_id,
                "object_id",
                suggestions=[
                    "Use ha_config_list_groups() to find existing group object_ids",
                    "Or provide a fresh object_id (without 'group.' prefix) to create a new group",
                ],
                context={"operation": "set_group"},
            )

            service_data = _build_group_service_data(
                object_id,
                name,
                icon,
                all_on,
                entities,
                add_entities,
                remove_entities,
            )

        except ToolFailure as error:
            raise _invalid_arguments(error) from error
        return await self.groups.set(service_data, wait=wait)

    async def ha_config_remove_group(
        self,
        object_id: Annotated[str, Field(description=_OBJECT_ID_DESCRIPTION)],
        wait: Annotated[
            bool,
            Field(
                description="Wait for group to be fully removed before returning.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Remove a service-based Home Assistant entity group via the group.remove service.

        **When NOT to use:** for groups created through `ha_config_set_helper(helper_type="group", ...)`,
        use `ha_remove_helpers_integrations`. Those config-entry-backed groups are not reachable via the
        group.remove service.

        **When to use:** removing groups created with `ha_config_set_group` or defined in YAML
        via `group:` configuration. Config-entry-backed deletion tools cannot find these.

        EXAMPLES:
        - Remove group: ha_config_remove_group("living_room_lights")

        Use ha_config_list_groups() to find existing groups.

        **WARNING:**
        - Removing a group used in automations may cause those automations to fail.
        - Groups defined in YAML can be removed at runtime but come back on group.reload
          or a restart.
        - This only removes old-style groups, not platform-specific groups.
        """

        try:
            validate_identifier_not_empty(
                object_id,
                "object_id",
                suggestions=[
                    "Use ha_config_list_groups() to find existing group object_ids"
                ],
                context={"operation": "remove_group"},
            )

        except ToolFailure as error:
            raise _invalid_arguments(error) from error
        return await self.groups.remove(object_id, wait=wait)
