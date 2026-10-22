"""Entity-removal input policy and storage-helper recovery guidance."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.runtime.validation import validate_identifier_not_empty


def removal_targets(entity_id: str | list[str]) -> tuple[list[str], bool]:
    if isinstance(entity_id, str):
        validate_identifier_not_empty(entity_id, "entity_id")
        return [entity_id], False
    if (
        not isinstance(entity_id, list)
        or not entity_id
        or not all(isinstance(item, str) for item in entity_id)
    ):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "entity_id must be a nonempty string or a nonempty list of entity IDs",
            )
        )
    if len(entity_id) > 100:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Too many entity IDs: {len(entity_id)} exceeds maximum of 100",
            )
        )
    for value in entity_id:
        validate_identifier_not_empty(value, "entity_id")
    return entity_id, True


def storage_note_unread(entity_id: str, reason: Any) -> list[str]:
    return [
        f"Whether {entity_id} belongs to a stored helper could not be read "
        f"({reason}); if it does, the helper stays and Home Assistant adds its "
        "entity again when it restarts."
    ]


def stored_helper_removal_note(
    entity_id: str, platform: str, storage_id: str
) -> list[str]:
    remove = (
        f"ha_remove_zone(zone_id={storage_id!r})"
        if platform == "zone"
        else (
            f"ha_remove_helpers_integrations(helper_type={platform!r}, target={storage_id!r}, confirm=True)"
        )
    )
    return [
        f"{entity_id} is the entity of the stored {platform} {storage_id!r}, "
        "which stays: Home Assistant adds its entity again when it restarts. "
        f"To remove the {platform} itself, use {remove}."
    ]
