"""Pure group input and response rules, independent of Core and its transports."""

from typing import Any

from .projection import build_pagination_metadata


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
            attributes = dict(state.get("attributes") or {})
            members = attributes.pop("entity_id", [])
            groups.append(
                {
                    "friendly_name": None,
                    "icon": None,
                    # Core sets ``auto`` only on group.set groups.
                    "auto": False,
                    **attributes,
                    **{k: v for k, v in state.items() if k != "attributes"},
                    "object_id": entity_id.removeprefix("group."),
                    "entity_ids": members,
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


def group_set_result(
    service_data: dict[str, Any], extra: dict[str, Any], *, existed: bool
) -> dict[str, Any]:
    object_id = service_data["object_id"]
    entity_id = f"group.{object_id}"
    return {
        "success": True,
        "entity_id": entity_id,
        "object_id": object_id,
        "updated_fields": [key for key in service_data if key != "object_id"],
        "message": f"Successfully {'updated' if existed else 'created'} group: {entity_id}",
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


def group_set_warnings(
    service_data: dict[str, Any], *, existed: bool, auto: bool
) -> list[str]:
    entity_id = f"group.{service_data['object_id']}"
    if not existed and "remove_entities" in service_data:
        return [
            f"{entity_id} did not exist, so Core created it without members; remove_entities had nothing to remove."
        ]
    if existed and not auto:
        return [
            f"{entity_id} is defined in YAML: group.reload and a restart rebuild it from YAML and drop this change."
        ]
    return []


def group_remove_warnings(entity_id: str, *, auto: bool) -> list[str]:
    return (
        []
        if auto
        else [
            f"{entity_id} is defined in YAML: group.reload and a restart bring it back."
        ]
    )
