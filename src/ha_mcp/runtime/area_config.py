"""Area/floor validation and post-write label confirmation policy."""

from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode, create_error_response

from .area_payloads import (
    build_area_create_payload,
    build_area_update_payload,
    build_floor_create_payload,
    build_floor_update_payload,
)
from .errors import raise_classified, raise_failure
from .registries import AreaDiscoveryTools
from .registry_references import validate_registry_ids
from .registry_write_context import RegistryWriteContext
from .validation import validate_identifier_not_empty


def _label_id_set(entry: dict[str, Any] | None) -> set[str]:
    """String label IDs stored on an area registry entry."""
    if not isinstance(entry, dict):
        return set()
    return {lbl for lbl in (entry.get("labels") or []) if isinstance(lbl, str)}


class AreaConfigTools(AreaDiscoveryTools, RegistryWriteContext):
    def _map_area_write_error(
        self, error: Exception, context: dict[str, Any], suggestions: list[str]
    ) -> NoReturn:
        raise_classified(error, context, suggestions=suggestions)

    async def _validate_area_refs(
        self, labels: list[str] | None, floor_id: str | None
    ) -> None:
        await validate_registry_ids(
            self.backend, self.context, None, labels, {}, floor_id=floor_id
        )

    async def _area_floor_change(
        self, kind: str, action: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._registry_change(
            kind,
            action,
            data,
            "ha_remove_area_or_floor" if action == "delete" else "ha_set_area_or_floor",
        )

    async def _list_area_rows(self) -> dict[str, Any]:
        snapshot = await self.backend.execute(
            "registries.snapshot", {"registries": ["area"]}, self.context
        )
        return {"success": True, "result": snapshot["areas"]}

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
                message = build_area_update_payload(
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
                message = build_area_create_payload(
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
                message = build_floor_update_payload(
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
                message = build_floor_create_payload(
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
            listed = await self._list_area_rows()
        except Exception as exc:  # noqa: BLE001
            # Write already succeeded; a transport failure on re-read must
            # not look like a failed create (retry would duplicate the area).
            raise_failure(
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
        raise_failure(
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
            **({"warnings": result["warnings"]} if result.get("warnings") else {}),
            result_key: data,
            id_key: returned_id,
            "kind": kind,
            "message": f"Successfully {operation}d {kind}: {display_name}",
        }
