"""Canonical Energy tool signature and request routing."""

from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.coercion import JSON_STRING_COERCION

from .errors import raise_failure


class EnergyFacade:
    energy: Any

    async def ha_manage_energy_prefs(
        self,
        mode: Annotated[
            Literal["get", "set", "add_device", "remove_device", "add_source"],
            Field(
                description=(
                    "Operation mode. Primitives: 'get' reads the current prefs; "
                    "'set' writes a full prefs payload."
                )
            ),
        ],
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Full prefs payload for mode='set'. Must contain the "
                    "top-level keys you intend to replace: 'energy_sources', "
                    "'device_consumption', 'device_consumption_water'. Any "
                    "omitted key is preserved. "
                    "Call with mode='get' first, mutate the returned config, "
                    "then pass the whole object back. Ignored by convenience "
                    "modes."
                ),
                default=None,
            ),
        ] = None,
        config_hash: Annotated[
            str | dict[str, str] | None,
            Field(
                description=(
                    "Hash from a previous mode='get' call. REQUIRED for mode='set' unless "
                    "dry_run=True. Two forms: str (full-blob lock) or dict (per-key lock, "
                    "taken from the config_hash_per_key field of mode='get'). Pass the dict"
                    " form as a native object, NOT a JSON-encoded string — a stringified "
                    "dict is treated as a full-blob token and will report RESOURCE_LOCKED; "
                    "clients that can only send strings should use the str full-blob form. "
                    "Ignored by convenience modes."
                ),
                default=None,
            ),
        ] = None,
        dry_run: Annotated[
            bool,
            Field(
                description=(
                    "If True, preview without saving. With the component, validates "
                    "the complete proposal through Core's registered save schema. "
                    "Without that capability, explicitly reports proposal validation "
                    "unavailable. Semantic energy/validate checks persisted state only."
                ),
                default=False,
            ),
        ] = False,
        stat_consumption: Annotated[
            str | None,
            Field(
                description=(
                    "Statistic entity_id for mode='add_device' / "
                    "'remove_device' (e.g. 'sensor.fridge_energy'). "
                    "Required for those modes; ignored otherwise."
                ),
                default=None,
            ),
        ] = None,
        name: Annotated[
            str | None,
            Field(
                description=("Display name for mode='add_device'; ignored otherwise."),
                default=None,
            ),
        ] = None,
        included_in_stat: Annotated[
            str | None,
            Field(
                description=(
                    "'Parent' statistic for mode='add_device'. Set this to a statistic that"
                    " already INCLUDES this device's consumption (e.g., a whole-home or "
                    "circuit-level meter that this device feeds into). The Energy Dashboard"
                    " will subtract this device's reading from the parent so the parent's "
                    "contribution is not double-counted. Ignored otherwise."
                ),
                default=None,
            ),
        ] = None,
        water: Annotated[
            bool,
            Field(
                description=(
                    "If True, mode='add_device' / 'remove_device' targets "
                    "'device_consumption_water' instead of 'device_consumption'."
                ),
                default=False,
            ),
        ] = False,
        source: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Single native energy_sources entry for mode='add_source'. "
                    "Use mode='get', include_schema=True to inspect the running "
                    "Core's fields and requirements; dry_run=True validates without saving."
                ),
                default=None,
            ),
        ] = None,
        include_schema: Annotated[
            bool,
            Field(
                description="With mode='get', describe the running Core's save schema. Unavailable without the component."
            ),
        ] = False,
        include_statistics: Annotated[
            bool,
            Field(
                description="With mode='get', include native recorder metadata and resolved output units for all configured statistic references. Ignored for other modes."
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Manage the Home Assistant Energy Dashboard preferences: grid / solar /
        battery / gas / water energy sources, device consumption sensors for
        electricity and water, and cost tariffs.

        WHEN NOT TO USE:
        - To create the underlying statistics themselves — they must already
          exist as HA entities before being referenced here; create them via
          the relevant integration's config flow first.

        WHEN TO USE:
        - mode='get' / 'set': inspect or replace the full Energy Dashboard
          config. Use 'set' for bulk edits or anything touching multiple
          top-level keys at once.
        - mode='add_device' / 'remove_device': add or remove a single
          device-consumption entry. The tool performs a fresh read-modify-write
          internally.
        - mode='add_source': append a single entry to ``energy_sources``.
          Same optimistic read-modify-write semantics.

        RELATED TOOLS: Use ha_get_history(source="statistics") with the returned
        statistic IDs for consumption, totals, and trends. Metadata comes directly
        from the running Core, including stored and display units.

        CAVEATS:
        - ``energy/save_prefs`` has per-key FULL-REPLACE semantics. Passing
          ``{"device_consumption": [<one entry>]}`` deletes every other device
          the user had configured — silently, with no error.
        - The per-key ``config_hash`` form lets an agent submit only the
          top-level key it wants to change: ``config`` keys must equal the dict
          keys, and a per-key submission still fully replaces
          that key's value. A mismatch on any locked key returns
          ``RESOURCE_LOCKED`` with the offending keys in ``mismatched_keys``.
        - ``dry_run=True`` skips the hash check entirely for both forms.
        - Writes require administrator access; Home Assistant rejects the save
          otherwise.
        - After a successful write, the tool calls ``energy/validate`` and
          returns residual issues (missing stats, unit mismatches) as
          ``post_save_validation_errors``; the save persists regardless —
          correct the config and write again if needed.
        - 'add_source' rejects duplicates by ``(type, stat_energy_from)`` for
          solar/battery/gas/water; grid entries are appended without a duplicate
          check (multiple grid variants are legitimate), so the caller
          de-duplicates grid sources.

        EXAMPLES:
        - ha_manage_energy_prefs(mode="get", include_schema=True)
        - Preview a device: ha_manage_energy_prefs(mode="add_device", stat_consumption="sensor.fridge_energy", dry_run=True)
        - Remove a device: ha_manage_energy_prefs(mode="remove_device", stat_consumption="sensor.fridge_energy")
        """
        result: dict[str, Any]
        if mode == "get":
            result = await self.energy.read(
                include_schema=include_schema, include_statistics=include_statistics
            )
            return result

        if mode == "add_device":
            result = await self.energy.add_device(
                stat_consumption=stat_consumption,
                name=name,
                included_in_stat=included_in_stat,
                water=water,
                dry_run=dry_run,
            )
            return result

        if mode == "remove_device":
            result = await self.energy.remove_device(
                stat_consumption=stat_consumption,
                water=water,
                dry_run=dry_run,
            )
            return result

        if mode == "add_source":
            result = await self.energy.add_source(source=source, dry_run=dry_run)
            return result

        # mode == "set"
        if config is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'config' is required when mode='set'",
                    context={"mode": mode},
                    suggestions=[
                        "Call ha_manage_energy_prefs(mode='get') first, mutate the returned config, pass it back",
                    ],
                )
            )

        if {"id", "type"} & config.keys():
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    "id and type are reserved WebSocket transport fields",
                )
            )

        if dry_run:
            result = await self.energy.preview(config)
            return result

        if config_hash is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'config_hash' is required when mode='set' and dry_run=False",
                    context={"mode": mode},
                    suggestions=[
                        "Call ha_manage_energy_prefs(mode='get') to obtain a fresh config_hash",
                        "Or call again with dry_run=True to validate without a hash",
                    ],
                )
            )

        result = await self.energy.save(config, config_hash)
        return result
