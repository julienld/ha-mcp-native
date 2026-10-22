"""Zone mutations preserve the shared target, validation and response policy."""

from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response, create_validation_error
from ha_mcp.tool_api import ToolFailure

from .errors import raise_failure
from .helper_storage import HelperStorageTools
from .storage_targets import stored_target
from .validation import validate_identifier_not_empty
from .zone_records import _zone_write_response
from .zones import ZoneReadTools


class ZoneWriteTools(ZoneReadTools, HelperStorageTools):
    # Unlike ha_config_set_helper, ha_set_zone allows Core to suffix a second
    # stored zone with the same display name.
    _check_create_name = False

    def _backup_identity(self, helper_type: str) -> dict[str, str]:
        return {"backup_domain": "zone", "source_tool": "ha_set_zone"}

    def _map_zone_write_error(
        self, error: Exception, zone_id: str | None, operation: str | None
    ) -> NoReturn:
        context: dict[str, Any] = {"zone_id": zone_id}
        if operation is not None:
            context["operation"] = operation
        raise_failure(
            create_error_response(ErrorCode.INTERNAL_ERROR, str(error), context=context)
        )

    async def _zone_target_snapshot(self, zone_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self.backend.execute(
            "zones.target.snapshot", {"zone_id": zone_id}, self.context
        )
        return result

    async def _zone_state(self, entity_id: str) -> dict[str, Any] | None:
        result = await self.backend.execute(
            "states.read", {"entity_ids": [entity_id]}, self.context
        )
        state: dict[str, Any] | None = result.get("states", {}).get(entity_id)
        return state

    async def _create_zone(self, *args: Any) -> dict[str, Any]:
        async with self._schema_context("zone", "create"):
            return await self._create_simple(*args)

    async def _update_zone(self, *args: Any) -> dict[str, Any]:
        # The public zone adapter historically orders entity_id before storage_id.
        helper_type, entity_id, storage_id, *remaining = args
        async with self._schema_context("zone", "update"):
            return await self._update_simple(
                helper_type, storage_id, entity_id, *remaining
            )

    async def _remove_stored_zone(
        self, storage_id: str, entity_id: str | None
    ) -> dict[str, Any]:
        from ha_mcp.utils.registry_update_lock import registry_update_lock

        async with registry_update_lock("helper_zone", storage_id):
            item = await self._stored_item("zone", storage_id)
            backup = await self.backend.execute(
                "helpers.backup.capture",
                {
                    "helper_type": "zone",
                    "action": "remove",
                    "backup_id": storage_id,
                    "item_id": item["item_id"],
                    "entity_id": entity_id,
                    "current_item": item["item"],
                    "backup_domain": "zone",
                    "source_tool": "ha_remove_zone",
                },
                self.context,
            )
            result: dict[str, Any] = await self.backend.execute(
                "zones.storage.delete",
                {"item_id": storage_id, "expected_item": item["item"]},
                self.context,
            )
        if backup and backup.get("warnings"):
            result.setdefault("warnings", []).extend(backup["warnings"])
        return result

    async def _wait_zone_removed(self, entity_id: str) -> bool:
        result: bool = await self.backend.execute(
            "entities.removal.wait", {"entity_id": entity_id}, self.context
        )
        return result

    async def _resolve_zone(self, zone_id: str) -> tuple[str | None, str, str | None]:
        """The entity_id, storage zone_id and registry icon of a stored zone,
        given its entity_id or zone_id.

        Resolved as ``stored_target`` resolves it, and so as the zone backups
        do: a bare ``zone_id`` is a storage id from ``zone/list``, and
        ``zone.<name>`` is the zone whose registry entry has that entity_id. A
        renamed entity can carry another zone's id as its name, so a bare id
        never names an entity, and a repeated removal deletes nothing.
        Both entity_id and icon are None for a stored zone without a registry
        entry; the icon is also None when the entry sets none. The
        home zone and YAML zones are not in the storage; Core gives them no
        ``unique_id``, so they have no registry entry.
        """
        snapshot = await self._zone_target_snapshot(zone_id)
        entries = snapshot["entries"]
        items = snapshot["items"]
        stored = {str(zone.get("id")) for zone in items}
        storage_id, entity = stored_target("zone", zone_id, stored, entries)
        if storage_id is not None and storage_id in stored:
            icon = next(
                (e.get("icon") for e in entries if e.get("unique_id") == storage_id),
                None,
            )
            return entity, storage_id, icon
        entity_id = zone_id if zone_id.startswith("zone.") else f"zone.{zone_id}"
        # A registry entry links a stored zone, so only a state without one is
        # a YAML or the home zone.
        linked = any(entry.get("entity_id") == entity_id for entry in entries)
        if not linked and await self._zone_state(entity_id) is not None:
            raise_failure(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Zone {entity_id} is not a stored zone: it is defined in YAML "
                    "or, for the default zone.home, by the home location in the "
                    "general settings, so the zone tools cannot change or remove it.",
                    context={"zone_id": zone_id, "entity_id": entity_id},
                    suggestions=[
                        "Edit a YAML zone in configuration.yaml",
                        "Change the home location under Settings > System > General",
                    ],
                )
            )
        raise_failure(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Zone not found: {zone_id}",
                context={"zone_id": zone_id},
                suggestions=[
                    "Use ha_get_zone() without zone_id to see all available zones",
                    (
                        "A zone_id without 'zone.' is a storage id; name a zone "
                        "by its entity with the full entity_id"
                    ),
                ],
            )
        )

    async def ha_set_zone(
        self,
        name: Annotated[
            str | None,
            Field(
                description="Display name for the zone",
                default=None,
            ),
        ] = None,
        latitude: Annotated[
            float | None,
            Field(
                description="Latitude coordinate of the zone center",
                default=None,
            ),
        ] = None,
        longitude: Annotated[
            float | None,
            Field(
                description="Longitude coordinate of the zone center",
                default=None,
            ),
        ] = None,
        zone_id: Annotated[
            str | None,
            Field(
                description="Zone ID or entity_id of the zone to update (from ha_get_zone)",
                default=None,
            ),
        ] = None,
        radius: Annotated[
            float | None,
            Field(
                description="Radius of the zone in meters; Home Assistant uses 100 when it is omitted on create",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:briefcase'). On update, '' clears it, except an icon stored in the zone itself (as the Home Assistant UI stores it; ha_get_zone shows it), which cannot be removed",
                default=None,
            ),
        ] = None,
        passive: Annotated[
            bool | None,
            Field(
                description="Passive mode: the zone is hidden in the frontend and not used for device tracker state, but automations can still use it (false when omitted on create)",
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool,
            Field(
                description="Wait for the zone to be queryable before returning. Set to False for bulk operations.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Create or update a Home Assistant zone.

        Omit zone_id to create a new zone (name, latitude, longitude required).
        Provide zone_id to update an existing zone (only specified fields change).
        Zones defined in YAML, and the home zone, cannot be changed here.

        EXAMPLES:
        - Create: ha_set_zone(name="Office", latitude=40.7128, longitude=-74.0060, radius=150, icon="mdi:briefcase")
        - Update: ha_set_zone(zone_id="office", radius=200)
        - Update by entity_id: ha_set_zone(zone_id="zone.office", radius=200)
        """
        operation = "create"
        try:
            # ``None`` stays the documented "create-new" sentinel; explicit
            # empty/whitespace ``zone_id`` would silently route to the
            # create branch below and surface "name, latitude, longitude
            # required" instead of the actual cause (unusable ``zone_id``).
            if zone_id is not None:
                validate_identifier_not_empty(
                    zone_id,
                    "zone_id",
                    suggestions=[
                        "Omit zone_id entirely to create a new zone",
                        "Pass a valid zone_id to update an existing zone",
                    ],
                    context={"action": "set"},
                )
            fields = {
                key: value
                for key, value in (
                    ("latitude", latitude),
                    ("longitude", longitude),
                    ("radius", radius),
                    ("passive", passive),
                )
                if value is not None
            }
            if name is not None and not name.strip():
                raise_failure(
                    create_validation_error(
                        "name cannot be blank.", context={"zone_id": zone_id}
                    )
                )
            if zone_id:
                operation = "update"
                updated = [
                    key
                    for key, value in (
                        ("name", name),
                        ("latitude", latitude),
                        ("longitude", longitude),
                        ("radius", radius),
                        ("icon", icon),
                        ("passive", passive),
                    )
                    if value is not None
                ]
                if not updated:
                    raise_failure(
                        create_validation_error(
                            "No fields to update. Provide at least one field to change.",
                            context={"zone_id": zone_id},
                        )
                    )
                entity_id, storage_id, registry_icon = await self._resolve_zone(zone_id)
                # The helper write path merges into the stored zone and keeps a
                # new icon in the registry, where it stays clearable, unless the
                # zone already stores one (#2643). It takes the storage id first.
                result = await self._update_zone(
                    "zone",
                    entity_id or storage_id,
                    storage_id,
                    name,
                    icon,
                    None,
                    None,
                    None,
                    wait,
                    False,
                    fields,
                )
                response = _zone_write_response(result, storage_id, "updated")
                # The reply holds the stored zone plus the registry fields this
                # write set; the entity shows its registry icon over the stored
                # one, as ha_get_zone reports it. An icon this write set or
                # cleared ('') is already in the reply.
                if icon is None and registry_icon:
                    response["zone_data"]["icon"] = registry_icon
                response["updated_fields"] = updated
                return response

            if name is None or latitude is None or longitude is None:
                raise_failure(
                    create_validation_error(
                        "name, latitude, and longitude are required when creating a zone.",
                    )
                )
            result = await self._create_zone(
                "zone",
                name,
                icon,
                None,
                None,
                None,
                wait,
                False,
                fields,
            )
            return _zone_write_response(result, None, "created")

        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001
            self._map_zone_write_error(e, zone_id, operation)

    async def ha_remove_zone(
        self,
        zone_id: Annotated[
            str,
            Field(description="Zone ID or entity_id of the zone to remove"),
        ],
        wait: Annotated[
            bool,
            Field(
                description="Wait for the zone to be gone before returning. Set to False for bulk operations.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Remove a Home Assistant zone.

        EXAMPLES:
        - Remove zone: ha_remove_zone("office")
        - Remove by entity_id: ha_remove_zone("zone.office")

        **WARNING:** Removing a zone used in automations may cause those automations to fail.
        Use ha_get_zone() to find the zone_id for the zone you want to remove.

        **NOTE:** Zones defined in YAML, and the home zone, cannot be removed here.
        """
        try:
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                zone_id,
                "zone_id",
                suggestions=["Use ha_get_zone() to find existing zone_ids"],
                context={"operation": "remove_zone"},
            )
            entity_id, storage_id, _ = await self._resolve_zone(zone_id)
            result = await self._remove_stored_zone(storage_id, entity_id)
            if not result.get("success"):
                raise_failure(
                    create_error_response(
                        zone_failure_code(result),
                        f"Failed to remove zone {entity_id or storage_id}: "
                        f"{result.get('error', 'Unknown error')}",
                        context={"zone_id": zone_id, "entity_id": entity_id},
                        suggestions=[
                            "Use ha_get_zone() without zone_id to see all available zones",
                        ],
                    )
                )
            response: dict[str, Any] = {
                "success": True,
                "zone_id": storage_id,
                "entity_id": entity_id,
                "message": f"Successfully removed zone: {entity_id or storage_id}",
            }
            if result.get("warnings"):
                response["warnings"] = list(result["warnings"])
            if entity_id is None:
                not_waited = ", so the removal was not waited on" if wait else ""
                response.setdefault("warnings", []).append(
                    f"Zone {storage_id} had no entity in the registry{not_waited}."
                )
                return response
            if not wait:
                return response
            try:
                if not await self._wait_zone_removed(entity_id):
                    response.setdefault("warnings", []).append(
                        f"Deletion confirmed but {entity_id} is still present "
                        "after the wait window."
                    )
            except (ToolFailure, PermissionError, TimeoutError, OSError) as e:
                response.setdefault("warnings", []).append(
                    f"Deletion confirmed but removal verification failed: {e}"
                )
            return response

        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001
            self._map_zone_write_error(e, zone_id, None)


def zone_failure_code(result: dict[str, Any]) -> ErrorCode:
    """Retain Core's missing-item classification on legacy failed envelopes."""
    return {
        "invalid_format": ErrorCode.VALIDATION_INVALID_PARAMETER,
        "not_found": ErrorCode.RESOURCE_NOT_FOUND,
    }.get(str(result.get("error_code", "")), ErrorCode.SERVICE_CALL_FAILED)
