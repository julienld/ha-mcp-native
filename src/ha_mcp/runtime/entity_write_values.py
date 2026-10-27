"""Pure target, bulk and merge policy for complete entity writes."""

import re
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.runtime.validation import validate_identifier_not_empty

from .entity_parameters import (
    _parse_aliases_param,
    _parse_categories_param,
    _parse_expose_to_param,
    _parse_options_param,
    _parse_string_list_field,
)


def _parse_set_entity_ids(
    entity_id: str | list[str],
) -> tuple[list[str], bool]:
    """Parse entity_id into (entity_ids, is_bulk). Raises on invalid input."""
    if isinstance(entity_id, str):
        return [entity_id], False
    elif isinstance(entity_id, list):
        if not entity_id:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "entity_id list cannot be empty",
                )
            )
        if not all(isinstance(e, str) for e in entity_id):
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "All entity_id values must be strings",
                )
            )
        return entity_id, len(entity_id) > 1
    else:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"entity_id must be string or list of strings, got {type(entity_id).__name__}",
            )
        )


def _validate_enabled_constraint(
    enabled: bool | None,
    entity_ids: list[str],
) -> None:
    """Block registry-disable on automation and script entities.

    Registry-disabling (enabled=False) removes the entity from the HA
    state machine entirely, making it invisible in the UI and
    unqueryable via state APIs until re-enabled AND the integration is
    reloaded.  For automations, use
    ha_config_set_automation(identifier=..., enabled=...) to control their
    runtime enabled state.  For scripts,
    ha_config_set_script(script_id=..., run="stop") only stops a currently
    running execution; it does not disable the script, and Home Assistant
    has no script runtime enable/disable service.
    """
    if enabled is False:
        blocked = [
            eid for eid in entity_ids if eid.split(".")[0] in ("automation", "script")
        ]
        if blocked:
            _domain = blocked[0].split(".")[0]
            if _domain == "script":
                message = (
                    "Cannot registry-disable script entities with "
                    "ha_set_entity(enabled=False). This removes the entity from the "
                    "state machine and hides it from the UI until it is re-enabled "
                    "and scripts are reloaded. Use ha_config_set_script("
                    f"script_id='{blocked[0]}', run='stop') only to stop a "
                    "currently running execution; it does not disable the script. "
                    "Home Assistant has no script runtime enable/disable service."
                )
                suggestions = [
                    "Use ha_config_set_script(script_id=..., run='stop') only to "
                    + "stop a currently running execution; it does not disable the script",
                    "Home Assistant has no script runtime enable/disable service",
                ]
            else:
                message = (
                    f"Cannot registry-disable {_domain} entities with "
                    "ha_set_entity(enabled=False). This removes the entity from the "
                    "state machine and hides it from the UI until it is re-enabled "
                    f"AND the {_domain}s are reloaded. Use "
                    f"ha_config_set_automation(identifier='{blocked[0]}', "
                    "enabled=False) instead to disable it without removing it."
                )
                suggestions = [
                    "Use ha_config_set_automation(identifier=..., enabled=...) to "
                    + "disable the automation (keeps it visible and manageable)",
                    "Use ha_config_set_automation(identifier=..., enabled=True) to "
                    + "re-enable it later",
                    "ha_set_entity(enabled=False) is for registry-level disable — it fully hides the entity",
                ]
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message,
                    suggestions=suggestions,
                )
            )


def apply_entity_rename(
    entity_id: str,
    new_entity_id: str,
    message: dict[str, Any],
    updates_made: list[str],
) -> None:
    """Validate and apply a rename to the update message. Raises on invalid input."""
    entity_pattern = r"^[a-z_]+\.[a-z0-9_]+$"
    if not re.match(entity_pattern, new_entity_id):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid new_entity_id format: {new_entity_id}",
                suggestions=[
                    "Use format: domain.object_id (lowercase letters, numbers, underscores only)"
                ],
                context={"new_entity_id": new_entity_id},
            )
        )
    current_domain = entity_id.split(".", maxsplit=1)[0]
    new_domain = new_entity_id.split(".", maxsplit=1)[0]
    if current_domain != new_domain:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Domain mismatch: cannot change from '{current_domain}' to '{new_domain}'",
                suggestions=[f"New entity_id must start with '{current_domain}.'"],
                context={
                    "entity_id": entity_id,
                    "new_entity_id": new_entity_id,
                },
            )
        )
    message["new_entity_id"] = new_entity_id
    updates_made.append(f"entity_id -> {new_entity_id}")


def parse_entity_write(
    entity_id: str | list[str], changes: dict[str, Any]
) -> tuple[list[str], bool, dict[str, Any]]:
    targets, bulk = _parse_set_entity_ids(entity_id)
    for target in targets:
        validate_identifier_not_empty(target, "entity_id")
    singles = [
        name
        for name in (
            "area_id",
            "name",
            "use_device_name",
            "icon",
            "device_class",
            "options",
            "enabled",
            "hidden",
            "aliases",
            "use_entity_name_alias",
            "new_entity_id",
            "new_device_name",
        )
        if changes.get(name) is not None
    ]
    if bulk and singles:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Bulk operations (multiple entity_ids) only support categories, labels, and expose_to. Single-entity parameters provided: {singles}",
                suggestions=["Use one entity_id for the other entity properties"],
            )
        )
    _validate_enabled_constraint(changes.get("enabled"), targets)
    return (
        targets,
        bulk,
        {
            **changes,
            "aliases": _parse_aliases_param(changes.get("aliases")),
            "categories": _parse_categories_param(changes.get("categories")),
            "labels": _parse_string_list_field(changes.get("labels"), "labels"),
            "options": _parse_options_param(changes.get("options")),
            "expose_to": _parse_expose_to_param(
                changes.get("expose_to"), validate_assistants=False
            ),
            "label_operation": changes.get("label_operation", "set"),
        },
    )


def merge_entity_labels(
    current: list[str], incoming: list[str] | None, operation: str
) -> list[str] | None:
    if incoming is None or operation not in {"add", "remove"}:
        return incoming
    if operation == "add":
        return list(dict.fromkeys([*current, *incoming]))
    removed = set(incoming)
    return [label for label in current if label not in removed]


def merge_entity_aliases(
    current: list[str | None],
    incoming: list[str | None] | None,
    use_entity_name: bool | None,
) -> tuple[list[str | None] | None, str | None]:
    if use_entity_name is None:
        if incoming is None or None in incoming:
            return incoming, None
        return (
            ([None, *incoming], "entity-name alias kept")
            if None in current
            else (incoming, None)
        )
    source = current if incoming is None else incoming
    strings: list[str | None] = [alias for alias in source if alias is not None]
    return (
        ([None, *strings], "entity-name alias on")
        if use_entity_name
        else (strings, "entity-name alias off")
    )


def _format_entity_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Format entity registry entry for API response."""
    return {
        "entity_id": entry.get("entity_id"),
        "name": entry.get("name"),
        "original_name": entry.get("original_name"),
        "icon": entry.get("icon"),
        "area_id": entry.get("area_id"),
        "disabled_by": entry.get("disabled_by"),
        "hidden_by": entry.get("hidden_by"),
        "aliases": entry.get("aliases", []),
        "labels": entry.get("labels", []),
        "categories": entry.get("categories", {}),
        "device_class": entry.get("device_class"),
        "original_device_class": entry.get("original_device_class"),
        "options": entry.get("options", {}),
    }


def entity_write_result(
    entity_id: str,
    original_entity_id: str,
    entity_entry: dict[str, Any],
    updates_made: list[str],
    new_entity_id: str | None,
    exposure_result: dict[str, bool] | None,
    device_rename_result: dict[str, Any] | None,
    device_name_warning: str | None = None,
    *,
    preserve_core_fields: bool = False,
) -> dict[str, Any]:
    """Build the final response dict for a single-entity update."""
    response_data: dict[str, Any] = {
        "success": True,
        "entity_id": entity_id,
        "updates": updates_made,
        "entity_entry": entity_entry
        if preserve_core_fields
        else _format_entity_entry(entity_entry),
        "message": f"Entity updated: {', '.join(updates_made)}",
    }

    # Include old_entity_id and rename warning when a rename was performed
    if new_entity_id is not None:
        response_data["old_entity_id"] = original_entity_id
        response_data.setdefault("warnings", []).append(
            "Remember to update any automations, scripts, or dashboards "
            "that reference the old entity_id"
        )

    if exposure_result is not None:
        response_data["exposure"] = exposure_result

    if device_name_warning is not None:
        response_data.setdefault("warnings", []).append(device_name_warning)

    if device_rename_result is not None:
        response_data["device_rename"] = device_rename_result
        # Return-shape contract: warnings are a top-level list as well.
        if device_rename_result.get("warnings"):
            response_data.setdefault("warnings", []).extend(
                device_rename_result["warnings"]
            )
        # Mark partial when a device rename was requested but didn't complete
        # for an operational reason: WS-call failure (device_id present + warnings)
        # or upstream registry lookup failure (lookup_failed marker). Not partial
        # when the entity simply has no device — that's a no-op, not an incomplete
        # operation.
        if device_rename_result.get("warnings") and (
            device_rename_result.get("device_id")
            or device_rename_result.get("lookup_failed")
        ):
            response_data["partial"] = True

    return response_data
