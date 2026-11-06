"""Area and floor authoring through one shared policy implementation."""

from contextlib import nullcontext
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import ToolFailure
from ha_mcp.tools.coercion import JSON_STRING_COERCION, UNSET, parse_string_list_param
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .area_config import AreaConfigTools
from .area_payloads import resolve_clearable, validate_cross_kind_params
from .errors import raise_failure
from .validation import validate_identifier_not_empty


class AreaWriteTools(AreaConfigTools):
    async def ha_set_area_or_floor(
        self,
        kind: Annotated[
            Literal["area", "floor"],
            Field(
                description="Which registry to operate on: 'area' for rooms, 'floor' for building levels",
            ),
        ],
        name: Annotated[
            str | None,
            Field(
                description="Name (required when creating; optional when updating, e.g., 'Living Room', 'Ground Floor')",
                default=None,
            ),
        ] = None,
        id: Annotated[  # noqa: A002
            str | None,
            Field(
                description="Existing area_id or floor_id to update (use ha_list_floors_areas to find IDs)",
                default=None,
            ),
        ] = None,
        floor_id: Annotated[
            str | None,
            Field(
                description="Floor assignment when kind='area' (null or empty string to clear; omit to leave unchanged).",
            ),
        ] = Field(default_factory=lambda: UNSET),
        level: Annotated[
            int | None,
            Field(
                description="Numeric level when kind='floor' (0=ground, 1=first, -1=basement).",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:sofa', 'mdi:home-floor-1'; null or empty string to remove; omit to leave unchanged)",
            ),
        ] = Field(default_factory=lambda: UNSET),
        aliases: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Alternative names for voice assistant recognition (e.g., ['lounge'], empty list to clear)",
                default=None,
            ),
        ] = None,
        picture: Annotated[
            str | None,
            Field(
                description="Picture URL when kind='area' (null or empty string to remove; omit to leave unchanged).",
            ),
        ] = Field(default_factory=lambda: UNSET),
        labels: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Label IDs when kind='area' (replaces the area's label set; "
                    "empty list to clear). Omit to leave labels unchanged. "
                    "Floors have no labels."
                ),
                default=None,
            ),
        ] = None,
        temperature_entity_id: Annotated[
            str | None,
            Field(
                description=(
                    "Sensor the area reports temperature from, when kind='area' "
                    "(a sensor.* entity with device_class temperature; null or "
                    "empty string to clear). Omit to leave unchanged."
                ),
            ),
        ] = Field(default_factory=lambda: UNSET),
        humidity_entity_id: Annotated[
            str | None,
            Field(
                description=(
                    "Sensor the area reports humidity from, when kind='area' "
                    "(a sensor.* entity with device_class humidity; null or "
                    "empty string to clear). Omit to leave unchanged."
                ),
            ),
        ] = Field(default_factory=lambda: UNSET),
    ) -> dict[str, Any]:
        """Create or update a Home Assistant area or floor.

        Pass kind='area' (with optional floor_id, picture, labels, temperature_entity_id,
        humidity_entity_id) or kind='floor' (with optional level).
        Provide name only to create a new entry; provide id to update an existing one.
        Cross-kind parameters (e.g., picture or labels under kind='floor') are rejected with VALIDATION_INVALID_PARAMETER.

        EXAMPLES:
        ha_set_area_or_floor(kind="area", name="Kitchen")
        ha_set_area_or_floor(kind="area", id="kitchen", floor_id="ground_floor")
        ha_set_area_or_floor(kind="area", id="kitchen", labels=["site_home"])
        ha_set_area_or_floor(kind="area", id="kitchen", temperature_entity_id="sensor.kitchen_temp", humidity_entity_id=None)
        ha_set_area_or_floor(kind="floor", name="Basement", level=-1)
        ha_set_area_or_floor(kind="floor", id="ground_floor", level=0)
        """
        operation = "create"
        try:
            try:
                parsed_aliases = parse_string_list_param(aliases, "aliases")
            except ValueError as e:
                return raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid aliases parameter: {e}",
                    )
                )

            try:
                parsed_labels = parse_string_list_param(labels, "labels")
            except ValueError as e:
                return raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid labels parameter: {e}",
                    )
                )

            # Explicit null clears (some MCP clients cannot send ""); omitted
            # (UNSET) leaves the field alone. Builders keep their None/"" idiom.
            floor_id = resolve_clearable(floor_id)
            icon = resolve_clearable(icon)
            picture = resolve_clearable(picture)
            temperature_entity_id = resolve_clearable(temperature_entity_id)
            humidity_entity_id = resolve_clearable(humidity_entity_id)

            validate_cross_kind_params(
                kind,
                level,
                floor_id,
                picture,
                parsed_labels,
                temperature_entity_id,
                humidity_entity_id,
            )

            # ``None`` stays the documented "create-new" sentinel; explicit
            # empty/whitespace would silently route to the ``if id:`` create
            # branch below and lose update intent.
            if id is not None:
                validate_identifier_not_empty(
                    id,
                    "id",
                    suggestions=[
                        "Omit id entirely to create a new entry",
                        "Pass a real area_id/floor_id to update an existing entry",
                    ],
                    context={"kind": kind},
                )

            message, result_key, id_key, operation, name = (
                self._build_area_or_floor_message(
                    kind,
                    id,
                    name,
                    floor_id,
                    level,
                    icon,
                    parsed_aliases,
                    picture,
                    parsed_labels,
                    temperature_entity_id,
                    humidity_entity_id,
                )
            )

            async with (
                registry_update_lock(kind, id) if id is not None else nullcontext()
            ):
                # Issue #2159: the area registry stores an unknown floor_id
                # verbatim, and an unknown label_id is dropped on the way in
                # (HA filters the set through the label registry) — both end as a
                # success envelope that does not match what was asked for, so
                # validate before writing. ``validate_cross_kind_params`` already
                # rejected floor_id/labels for kind='floor', so this only ever
                # runs for areas; None and "" / [] (clear) skip the lookup.
                await self._validate_area_refs(parsed_labels, floor_id)

                result = await self._area_floor_change(kind, operation, message)

                if result.get("success"):
                    return await self._area_or_floor_write_success(
                        result,
                        result_key=result_key,
                        id_key=id_key,
                        identifier=id,
                        kind=kind,
                        operation=operation,
                        name=name,
                        parsed_labels=parsed_labels,
                    )

            error = result.get("error", {})
            error_msg = (
                error.get("message", str(error))
                if isinstance(error, dict)
                else str(error)
            )
            ctx: dict[str, Any] = {"operation": operation, "kind": kind}
            if name:
                ctx["name"] = name
            if id:
                ctx[id_key] = id
            return raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to {operation} {kind}: {error_msg}",
                    context=ctx,
                )
            )

        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001
            suggestions = [
                "Check Home Assistant connection",
                "For create: Verify the name is unique",
                f"For update: Verify the {kind} id exists using ha_list_floors_areas()",
            ]
            if kind == "area":
                suggestions.append("If assigning to a floor, verify floor_id exists")
                suggestions.append(
                    "If assigning labels, verify label IDs with ha_config_get_label()"
                )
            return self._map_area_write_error(
                e,
                context={"operation": operation, "kind": kind, "name": name, "id": id},
                suggestions=suggestions,
            )

    async def ha_remove_area_or_floor(
        self,
        kind: Annotated[
            Literal["area", "floor"],
            Field(description="Which registry to delete from: 'area' or 'floor'"),
        ],
        id: Annotated[  # noqa: A002
            str,
            Field(
                description="Area ID or floor ID to delete (use ha_list_floors_areas to find IDs)"
            ),
        ],
    ) -> dict[str, Any]:
        """Remove a Home Assistant area or floor.

        Removing an area unassigns its entities and devices (the entities and
        devices themselves are not removed). Removing a floor unassigns its
        areas. May break automations referencing the removed area/floor.
        """
        id_key = "area_id" if kind == "area" else "floor_id"
        try:
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                id,
                "id",
                suggestions=[
                    f"Pass a valid {id_key} (use ha_list_floors_areas() to list)",
                ],
                context={"action": "remove", "kind": kind},
            )
            message: dict[str, Any] = {
                id_key: id,
            }

            result = await self._area_floor_change(kind, "delete", message)

            if result.get("success"):
                return {
                    "success": True,
                    **(
                        {"warnings": result["warnings"]}
                        if result.get("warnings")
                        else {}
                    ),
                    id_key: id,
                    "kind": kind,
                    "message": f"Successfully removed {kind}: {id}",
                }

            error = result.get("error", {})
            error_msg = (
                error.get("message", str(error))
                if isinstance(error, dict)
                else str(error)
            )
            return raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to remove {kind}: {error_msg}",
                    context={"kind": kind, id_key: id},
                )
            )

        except ToolFailure as error:
            if error.code == "RESOURCE_NOT_FOUND" and error.status == "not_started":
                # Native preflight keeps its precise owner failure; this public
                # delete tool historically exposes SERVICE_CALL_FAILED.
                failure_message = f"Failed to remove {kind}: {error}"
                raise ToolFailure(
                    ErrorCode.SERVICE_CALL_FAILED.value,
                    failure_message,
                    data=create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        failure_message,
                        context={"kind": kind, id_key: id},
                    ),
                    status=error.status,
                ) from error
            raise
        except Exception as e:  # noqa: BLE001
            return self._map_area_write_error(
                e,
                context={"kind": kind, id_key: id},
                suggestions=[
                    "Check Home Assistant connection",
                    f"Verify the {kind} id exists using ha_list_floors_areas()",
                ],
            )
