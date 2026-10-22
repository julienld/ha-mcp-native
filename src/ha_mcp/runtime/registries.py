"""Shared registry discovery; only native/remote acquisition differs."""

from __future__ import annotations

import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response, create_validation_error
from ha_mcp.tool_api import BackendTools, ToolFailure
from ha_mcp.tools.coercion import JSON_STRING_COERCION, parse_string_list_param

from .errors import raise_failure
from .projection import project_fields, project_records, result_fields_warning

logger = logging.getLogger(__name__)


def _parse_projection_params(
    fields: str | list[str] | None,
    area_fields: str | list[str] | None,
) -> tuple[list[str] | None, list[str] | None]:
    """Validate the fields/area_fields projection params, raising on a bad shape."""
    parsed_fields: list[str] | None = None
    if fields is not None:
        try:
            parsed_fields = parse_string_list_param(fields, "fields", allow_csv=True)
            if parsed_fields is not None and len(parsed_fields) == 0:
                raise ValueError("fields must contain at least one key")
        except ValueError as exc:
            raise_failure(create_validation_error(str(exc), parameter="fields"))
    parsed_area_fields: list[str] | None = None
    if area_fields is not None:
        try:
            parsed_area_fields = parse_string_list_param(
                area_fields, "area_fields", allow_csv=True
            )
            if parsed_area_fields is not None and len(parsed_area_fields) == 0:
                raise ValueError("area_fields must contain at least one key")
        except ValueError as exc:
            raise_failure(create_validation_error(str(exc), parameter="area_fields"))
    return parsed_fields, parsed_area_fields


def _partition_areas_by_floor(
    areas: list[dict[str, Any]], valid_floor_ids: set[Any]
) -> tuple[
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Partition areas into (floor_map, unassigned_areas, orphaned_areas).

    Partitions areas into three disjoint sets:
      - nested:    floor_id present AND points to a known floor
      - orphaned:  floor_id present BUT points to a non-existent floor
                   (race between the concurrent reads, or manual
                   .storage inconsistency)
      - unassigned: no floor_id at all
    Orphaned is surfaced as a separate key so the LLM can diagnose
    registry drift without introspecting individual area fields.
    Use `is None` rather than falsy-check so that a floor_id of ""
    (valid but unusual) is treated as orphaned if it does not resolve,
    not as unassigned.
    """
    floor_map: dict[str, list[dict[str, Any]]] = {}
    unassigned_areas: list[dict[str, Any]] = []
    orphaned_areas: list[dict[str, Any]] = []
    for area in areas:
        fid = area.get("floor_id")
        if fid is None:
            unassigned_areas.append(area)
        elif fid in valid_floor_ids:
            floor_map.setdefault(fid, []).append(area)
        else:
            orphaned_areas.append(area)
    return floor_map, unassigned_areas, orphaned_areas


def _floor_sort_key(floor: dict[str, Any]) -> int:
    raw = floor.get("level")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            f"Floor {floor.get('floor_id')!r} has non-numeric "
            f"level {raw!r}; treating as 0 for sort"
        )
        return 0


class AreaDiscoveryTools(BackendTools):
    async def _fetch_area_floor_registries(
        self, progress: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        result = await self.backend.execute(
            "registries.snapshot", {"registries": ["area", "floor"]}, self.context
        )
        progress["phase"] = "registries_fetched"
        return result["areas"], result["floors"]

    def _raise_list_error(self, error: Exception, progress: dict[str, Any]) -> NoReturn:
        raise_failure(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                str(error),
                context=progress,
                suggestions=["Check Home Assistant's registry availability"],
            )
        )

    async def ha_list_floors_areas(
        self,
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Return only the specified top-level response keys to reduce response "
                    'size (e.g. ["floors"]). None = full response. Available keys: success,'
                    " floor_count, area_count, unassigned_count, orphaned_count, floors, "
                    "unassigned_areas, orphaned_areas, message."
                ),
            ),
        ] = None,
        area_fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Project each area record (in floors[].areas, unassigned_areas, and "
                    'orphaned_areas) to only the specified keys. E.g. ["area_id", "name"] '
                    "returns slim area records. None = full records. Unknown keys yield "
                    "empty records. Available keys: area_id, name, icon, floor_id, aliases,"
                    " picture, labels."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        List floors sorted by level ascending, each with their assigned areas nested, plus areas without a floor.

        Use for location-based reasoning where floor-to-area relationships matter, such as "which rooms are on the ground floor" or operations scoped to a level.

        Floors with level=None sort alongside level 0 (ground floor). Areas without a floor assignment appear in unassigned_areas; areas whose floor_id points to a non-existent floor appear in orphaned_areas. When the ha_mcp_tools component's registries capability is available, both registries come from a single in-process snapshot, so this classification is always consistent. Without it (legacy path), the two registries are fetched via independent WebSocket calls and a registry change between reads may transiently misclassify an area.
        """
        # Validate projection params before any WS round-trips so a bad shape
        # fails fast without burning two registry reads.
        parsed_fields, parsed_area_fields = _parse_projection_params(
            fields, area_fields
        )

        progress: dict[str, Any] = {
            "operation": "list_floors_areas",
            "phase": "start",
        }
        try:
            areas, floors = await self._fetch_area_floor_registries(progress)

            valid_floor_ids = {
                f.get("floor_id") for f in floors if f.get("floor_id") is not None
            }
            floor_map, unassigned_areas, orphaned_areas = _partition_areas_by_floor(
                areas, valid_floor_ids
            )
            progress["phase"] = "partitioned"

            # Build nested hierarchy, preserving all floor-registry fields for
            # forward compatibility with future HA Core additions
            topology = [
                {
                    **floor,
                    "areas": floor_map.get(fid, [])
                    if (fid := floor.get("floor_id")) is not None
                    else [],
                }
                for floor in floors
            ]

            topology.sort(key=_floor_sort_key)
            progress["phase"] = "sorted"

            # Apply per-area projection across all 3 buckets uniformly.
            # Snapshot pre-projection areas for the typo-guard warning.
            _orig_all_areas = list(areas)
            if parsed_area_fields is not None:
                for floor in topology:
                    floor["areas"] = project_records(floor["areas"], parsed_area_fields)
                unassigned_areas = project_records(unassigned_areas, parsed_area_fields)
                orphaned_areas = project_records(orphaned_areas, parsed_area_fields)

            response: dict[str, Any] = {
                "success": True,
                "floor_count": len(topology),
                "area_count": len(_orig_all_areas),
                "unassigned_count": len(unassigned_areas),
                "orphaned_count": len(orphaned_areas),
                "floors": topology,
                "unassigned_areas": unassigned_areas,
                "orphaned_areas": orphaned_areas,
                "message": (
                    f"Found {len(topology)} floor(s), {len(_orig_all_areas)} area(s), "
                    f"{len(unassigned_areas)} unassigned, "
                    f"{len(orphaned_areas)} orphaned"
                ),
            }

            # Typo-guard: combine projected areas across buckets to detect the
            # all-empty-records situation that signals an unknown area_fields key.
            if parsed_area_fields is not None:
                _projected_all = (
                    [a for f in topology for a in f["areas"]]
                    + unassigned_areas
                    + orphaned_areas
                )
                _warn = result_fields_warning(
                    _orig_all_areas,
                    _projected_all,
                    parsed_area_fields,
                    param_name="area_fields",
                )
                if _warn:
                    response.setdefault("warnings", []).append(_warn)

            return project_fields(response, parsed_fields)

        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Error listing floors and areas in phase {progress['phase']!r}: {e} "
                f"(progress={progress})"
            )
            self._raise_list_error(e, progress)
