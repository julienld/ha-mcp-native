"""
Area and floor management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant areas and floors - essential organizational features for smart homes.
"""

import asyncio
import logging
from contextlib import nullcontext
from typing import Annotated, Any, Literal, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..errors import ErrorCode, create_error_response
from ..runtime.registries import AreaDiscoveryTools
from ..utils.registry_update_lock import registry_update_lock
from .area_messages import (
    build_area_create_message,
    build_area_update_message,
    build_floor_create_message,
    build_floor_update_message,
    resolve_clearable,
    validate_cross_kind_params,
)
from .auto_backup import with_auto_backup
from .coercion import JSON_STRING_COERCION, UNSET, parse_string_list_param
from .component_registries import fetch_registries_via_component
from .config_helpers.registry import validate_registry_ids
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)


def _label_id_set(entry: dict[str, Any] | None) -> set[str]:
    """String label IDs stored on an area registry entry."""
    if not isinstance(entry, dict):
        return set()
    return {lbl for lbl in (entry.get("labels") or []) if isinstance(lbl, str)}


class AreaTools(AreaDiscoveryTools):
    """Area and floor management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    ha_list_floors_areas = tool(
        name="ha_list_floors_areas",
        tags={"Areas & Floors"},
        annotations=read_only_hints("List Floors and Areas", open_world=False),
    )(log_tool_usage(legacy_errors(AreaDiscoveryTools.ha_list_floors_areas)))

    def _raise_list_error(self, error: Exception, progress: dict[str, Any]) -> NoReturn:
        if isinstance(error, ToolError):
            raise error
        exception_to_structured_error(
            error,
            context=progress,
            suggestions=[
                "Check Home Assistant connection",
                "Verify WebSocket connection is active",
            ],
        )

    async def _fetch_area_floor_registries(
        self, progress: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return ``(areas, floors)`` as one consistent snapshot when possible.

        Prefers the ``ha_mcp_tools`` component's ``registries`` capability — a
        single in-process read of both registries, closing the TOCTOU window
        between two independent list calls. Falls back to the legacy 2-call
        ``asyncio.gather`` when the component is unavailable, downgraded, or
        errors on this frame. Updates ``progress["phase"]`` for the caller's
        error-context reporting either way.
        """
        component_result = await fetch_registries_via_component(
            self._client, ["area", "floor"]
        )
        if component_result is not None:
            progress["phase"] = "registries_fetched"
            return (
                component_result.get("areas") or [],
                component_result.get("floors") or [],
            )

        # Fetch both registries concurrently. Sequential awaits add a
        # round-trip per call on the WS transport; gather halves the
        # tool-side latency. Use return_exceptions=True so a failure on
        # one side doesn't cancel the other — the post-fetch guard
        # below reports both registries' state in the error context for
        # diagnosis. Indexed access + explicit annotations rather than
        # tuple-unpack — gather returns list[Any] which mypy can't
        # statically narrow to a 2-tuple.
        results = await asyncio.gather(
            self._client.send_websocket_message({"type": "config/area_registry/list"}),
            self._client.send_websocket_message({"type": "config/floor_registry/list"}),
            return_exceptions=True,
        )
        progress["phase"] = "registries_fetched"

        # Re-raise transport-level exceptions from either fetch so the
        # outer except handler classifies them via exception_to_structured_error.
        if isinstance(results[0], BaseException):
            raise results[0]
        if isinstance(results[1], BaseException):
            raise results[1]
        areas_result: dict[str, Any] = results[0]
        floors_result: dict[str, Any] = results[1]

        # A response with success=True but no "result" key is malformed —
        # treat it as a service call failure rather than silently returning
        # floor_count=0, area_count=0 on a populated instance.
        areas_ok = areas_result.get("success") and "result" in areas_result
        floors_ok = floors_result.get("success") and "result" in floors_result
        if not (areas_ok and floors_ok):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Failed to retrieve area or floor registry",
                    context={
                        "areas_success": areas_result.get("success"),
                        "floors_success": floors_result.get("success"),
                        "areas_response_keys": sorted(areas_result.keys()),
                        "floors_response_keys": sorted(floors_result.keys()),
                    },
                    suggestions=[
                        "Check Home Assistant connection",
                        "Verify WebSocket connection is active",
                    ],
                )
            )

        return areas_result["result"], floors_result["result"]

    def _build_area_or_floor_message(
        self,
        kind: str,
        identifier: str | None,
        name: str | None,
        floor_id: str | None,
        level: int | None,
        icon: str | None,
        parsed_aliases: list[str] | None,
        picture: str | None,
        parsed_labels: list[str] | None,
        temperature_entity_id: str | None = None,
        humidity_entity_id: str | None = None,
    ) -> tuple[dict[str, Any], str, str, str, str | None]:
        """Build the WS message plus (result_key, id_key, operation, name) for a set.

        ``name`` is returned because the create branches narrow it from
        ``str | None`` to ``str`` via ``validate_identifier_not_empty``.
        """
        if kind == "area":
            if identifier:
                message = build_area_update_message(
                    identifier,
                    name,
                    floor_id,
                    icon,
                    parsed_aliases,
                    picture,
                    parsed_labels,
                    temperature_entity_id,
                    humidity_entity_id,
                )
                operation = "update"
            else:
                # Reassignment narrows ``name`` from ``str | None`` to
                # ``str`` for the build-message call below.
                name = validate_identifier_not_empty(
                    name,
                    "name",
                    message="name is required when creating a new area",
                    context={"operation": "create_area"},
                    suggestions=["Provide a non-empty name for the new area"],
                )
                message = build_area_create_message(
                    name,
                    floor_id,
                    icon,
                    parsed_aliases,
                    picture,
                    parsed_labels,
                    temperature_entity_id,
                    humidity_entity_id,
                )
                operation = "create"
            result_key = "area"
            id_key = "area_id"
        else:  # kind == "floor"
            if identifier:
                message = build_floor_update_message(
                    identifier,
                    name,
                    level,
                    icon,
                    parsed_aliases,
                )
                operation = "update"
            else:
                name = validate_identifier_not_empty(
                    name,
                    "name",
                    message="name is required when creating a new floor",
                    context={"operation": "create_floor"},
                    suggestions=["Provide a non-empty name for the new floor"],
                )
                message = build_floor_create_message(
                    name,
                    level,
                    icon,
                    parsed_aliases,
                )
                operation = "create"
            result_key = "floor"
            id_key = "floor_id"
        return message, result_key, id_key, operation, name

    @staticmethod
    def _label_verify_context(
        *,
        parsed_labels: list[str],
        kind: str,
        returned_id: str | None,
        operation: str,
        name: str | None,
        write_committed: bool = False,
    ) -> dict[str, Any]:
        """Error context shared by both label-verification failure paths."""
        ctx: dict[str, Any] = {
            "operation": operation,
            "kind": kind,
            "area_id": returned_id,
            "expected_labels": parsed_labels,
        }
        if write_committed:
            ctx["write_committed"] = True
        if name:
            ctx["name"] = name
        return ctx

    async def _verify_area_labels_written(
        self,
        *,
        parsed_labels: list[str] | None,
        kind: str,
        data: dict[str, Any],
        returned_id: str | None,
        operation: str,
        name: str | None,
    ) -> None:
        """Confirm HA stored the requested label set, not just a success ack.

        Home Assistant answers an area create/update with ``success`` even when
        it dropped label IDs it did not recognise, so the ack alone is not
        evidence the set was applied. The response entry is checked first; the
        registry is re-read only when that entry disagrees, which also covers
        an ack that carries no labels at all.

        Both failure paths report ``write_committed: True``: the area write
        itself has already landed by the time this runs, so a caller that
        retries a create would end up with a duplicate area. Only the labels
        are still open, and those are fixed with ha_set_area_or_floor.
        """
        if kind != "area" or parsed_labels is None:
            return
        expected = set(parsed_labels)
        if _label_id_set(data) == expected:
            return
        try:
            listed = await self._client.send_websocket_message(
                {"type": "config/area_registry/list"}
            )
        except Exception as exc:  # noqa: BLE001
            # Write already succeeded; a transport failure on re-read must
            # not look like a failed create (retry would duplicate the area).
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Area write succeeded, but label verification failed",
                    details=str(exc),
                    context=self._label_verify_context(
                        parsed_labels=parsed_labels,
                        kind=kind,
                        returned_id=returned_id,
                        operation=operation,
                        name=name,
                        write_committed=True,
                    ),
                    suggestions=[
                        "The area write already committed; do not retry create.",
                        "Re-read with ha_list_floors_areas() before retrying labels.",
                    ],
                )
            )
        rows = listed.get("result") if listed.get("success") else None
        found: dict[str, Any] | None = None
        if isinstance(rows, list):
            found = next(
                (
                    a
                    for a in rows
                    if isinstance(a, dict) and a.get("area_id") == returned_id
                ),
                None,
            )
        if found is not None and _label_id_set(found) == expected:
            return
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Area write succeeded but the returned entry does not contain "
                "the requested labels",
                context=self._label_verify_context(
                    parsed_labels=parsed_labels,
                    kind=kind,
                    returned_id=returned_id,
                    operation=operation,
                    name=name,
                    write_committed=True,
                ),
                suggestions=[
                    "The area write already committed; do not retry create.",
                    (
                        "Apply the labels on their own with ha_set_area_or_floor("
                        + f"kind='area', id={returned_id!r}, "
                        + f"labels={parsed_labels!r})."
                    ),
                    (
                        "Verify the label IDs exist with ha_config_get_label() "
                        + "— Home Assistant drops unknown ones silently."
                    ),
                ],
            )
        )

    async def _area_or_floor_write_success(
        self,
        result: dict[str, Any],
        *,
        result_key: str,
        id_key: str,
        identifier: str | None,
        kind: str,
        operation: str,
        name: str | None,
        parsed_labels: list[str] | None,
    ) -> dict[str, Any]:
        data = result.get("result", {})
        if not isinstance(data, dict):
            data = {}
        returned_id = data.get(id_key, identifier)
        await self._verify_area_labels_written(
            parsed_labels=parsed_labels,
            kind=kind,
            data=data,
            returned_id=returned_id if isinstance(returned_id, str) else identifier,
            operation=operation,
            name=name,
        )
        display_name = name or data.get("name", returned_id)
        return {
            "success": True,
            result_key: data,
            id_key: returned_id,
            "kind": kind,
            "message": f"Successfully {operation}d {kind}: {display_name}",
        }

    # ============================================================
    # COMBINED SET / REMOVE
    # ============================================================

    @tool(
        name="ha_set_area_or_floor",
        tags={"Areas & Floors"},
        annotations=write_hints(
            "Create or Update Area or Floor",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
    @with_auto_backup(
        domain="area_or_floor",
        # Return "" when either discriminator is missing so the capture
        # pipeline skips cleanly. ``f"{a}:{b}"`` would otherwise produce
        # a truthy literal `":"` and trigger a useless lookup.
        id_fn=lambda kw: (
            f"{kw['kind']}:{kw['id']}" if kw.get("kind") and kw.get("id") else ""
        ),
    )
    @log_tool_usage
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
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid aliases parameter: {e}",
                    )
                )

            try:
                parsed_labels = parse_string_list_param(labels, "labels")
            except ValueError as e:
                raise_tool_error(
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
                await validate_registry_ids(
                    self._client,
                    None,
                    parsed_labels,
                    None,
                    floor_id=floor_id,
                    fail_closed=True,
                )

                result = await self._client.send_websocket_message(message)

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
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to {operation} {kind}: {error_msg}",
                    context=ctx,
                )
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error {operation} {kind} {name!r}: {e}")
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
            exception_to_structured_error(
                e,
                context={"operation": operation, "kind": kind, "name": name, "id": id},
                suggestions=suggestions,
            )
            return None  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    @tool(
        name="ha_remove_area_or_floor",
        tags={"Areas & Floors"},
        annotations=write_hints(
            "Remove Area or Floor", destructive=True, idempotent=True, open_world=False
        ),
    )
    @with_auto_backup(
        domain="area_or_floor",
        id_fn=lambda kw: (
            f"{kw['kind']}:{kw['id']}" if kw.get("kind") and kw.get("id") else ""
        ),
    )
    @log_tool_usage
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
        registry = "area_registry" if kind == "area" else "floor_registry"
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
                "type": f"config/{registry}/delete",
                id_key: id,
            }

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
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
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to remove {kind}: {error_msg}",
                    context={"kind": kind, id_key: id},
                )
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error removing {kind} {id!r}: {e}")
            exception_to_structured_error(
                e,
                context={"kind": kind, id_key: id},
                suggestions=[
                    "Check Home Assistant connection",
                    f"Verify the {kind} id exists using ha_list_floors_areas()",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


def register_area_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant area and floor management tools."""
    register_tool_methods(mcp, AreaTools(client))
