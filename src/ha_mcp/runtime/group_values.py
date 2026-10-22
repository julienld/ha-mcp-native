"""Pure group input and response rules, independent of Core and its transports."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure
from .projection import build_pagination_metadata


def _validate_group_params(
    object_id: str,
    entities: list[str] | None,
    add_entities: list[str] | None,
    remove_entities: list[str] | None,
) -> None:
    """Validate group parameters: object_id format, mutual exclusivity, and non-empty lists."""
    # Validate object_id doesn't contain invalid characters
    if "." in object_id:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid object_id: '{object_id}'. Do not include 'group.' prefix or dots.",
                context={"object_id": object_id},
                suggestions=["Provide object_id without 'group.' prefix or dots"],
            )
        )

    # Check mutual exclusivity of entity operations
    entity_ops = [
        ("entities", entities),
        ("add_entities", add_entities),
        ("remove_entities", remove_entities),
    ]
    provided_ops = [(op_name, val) for op_name, val in entity_ops if val is not None]

    if len(provided_ops) > 1:
        op_names = [op_name for op_name, _ in provided_ops]
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Only one of entities, add_entities, or remove_entities can be provided. Got: {op_names}",
                context={"object_id": object_id, "provided_ops": op_names},
                suggestions=[
                    "Use only one of: entities, add_entities, or remove_entities"
                ],
            )
        )

    # Validate non-empty lists
    if entities is not None and not entities:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Entities list cannot be empty",
                context={"object_id": object_id},
                suggestions=["Provide at least one entity ID in the entities list"],
            )
        )
    if add_entities is not None and not add_entities:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "add_entities list cannot be empty",
                context={"object_id": object_id},
                suggestions=["Provide at least one entity ID in the add_entities list"],
            )
        )


def _build_group_service_data(
    object_id: str,
    name: str | None,
    icon: str | None,
    all_on: bool | None,
    entities: list[str] | None,
    add_entities: list[str] | None,
    remove_entities: list[str] | None,
) -> dict[str, Any]:
    """Build service data dict for group.set service call."""
    service_data: dict[str, Any] = {
        "object_id": object_id,
    }
    if name is not None:
        service_data["name"] = name
    if icon is not None:
        service_data["icon"] = icon
    if all_on is not None:
        service_data["all"] = all_on
    if entities is not None:
        service_data["entities"] = entities
    if add_entities is not None:
        service_data["add_entities"] = add_entities
    if remove_entities is not None:
        service_data["remove_entities"] = remove_entities
    return service_data


def group_list_result(
    states: list[dict[str, Any]], limit: int, offset: int
) -> dict[str, Any]:
    groups = []
    for state in states:
        entity_id = state.get("entity_id", "")
        if entity_id.startswith("group."):
            attributes = state.get("attributes", {})
            groups.append(
                {
                    "entity_id": entity_id,
                    "object_id": entity_id.removeprefix("group."),
                    "state": state.get("state"),
                    "friendly_name": attributes.get("friendly_name"),
                    "icon": attributes.get("icon"),
                    "entity_ids": attributes.get("entity_id", []),
                    "all": attributes.get("all", False),
                    "order": attributes.get("order"),
                }
            )

    # Sort by friendly name, then entity_id: the unique tiebreaker keeps
    # groups with the same name in a stable order, so a page boundary
    # cannot duplicate or skip one when get_states() order shifts.
    groups.sort(
        key=lambda g: (
            (g.get("friendly_name") or g.get("entity_id", "")).lower(),
            g.get("entity_id", ""),
        )
    )

    total_count = len(groups)
    paginated_groups = groups[offset : offset + limit]

    return {
        "success": True,
        "groups": paginated_groups,
        **build_pagination_metadata(total_count, offset, limit, len(paginated_groups)),
        "message": f"Found {total_count} group(s)",
    }


def group_action_word(service_data: dict[str, Any]) -> str:
    is_create = (
        "entities" in service_data
        and "name" not in service_data
        and "add_entities" not in service_data
        and "remove_entities" not in service_data
    )
    return "created" if is_create else "updated"


def group_set_result(
    service_data: dict[str, Any], extra: dict[str, Any]
) -> dict[str, Any]:
    object_id = service_data["object_id"]
    entity_id = f"group.{object_id}"
    return {
        "success": True,
        "entity_id": entity_id,
        "object_id": object_id,
        "updated_fields": [key for key in service_data if key != "object_id"],
        "message": f"Successfully {group_action_word(service_data)} group: {entity_id}",
        **extra,
    }


def group_remove_result(object_id: str, extra: dict[str, Any]) -> dict[str, Any]:
    entity_id = f"group.{object_id}"
    return {
        "success": True,
        "entity_id": entity_id,
        "object_id": object_id,
        "message": f"Successfully removed group: {entity_id}",
        **extra,
    }
