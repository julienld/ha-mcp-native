"""One helper write signature and orchestration for both backend families."""

import logging
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api.parameters import HIDDEN_PARAM, BestPracticeKeyParam
from ha_mcp.tools.coercion import JSON_STRING_COERCION, parse_string_list_param

from .errors import raise_failure
from .helper_payload import core_fields
from .helper_schema import (
    _SIMPLE_CONFIG_KEYS_DESCRIPTION,
    SIMPLE_HELPER_TYPES,
    StorageHelperType,
)
from .helper_storage import HelperStorageTools
from .validation import clear_or_keep

_LOGGER = logging.getLogger(__name__)


class HelperConfigTools(HelperStorageTools):
    async def ha_config_set_helper(
        self,
        helper_type: Annotated[
            StorageHelperType | Literal["config_subentry"] | str,
            Field(description="Type of helper to create or update."),
        ],
        name: Annotated[
            str | None,
            Field(
                description=(
                    "Display name for simple/flow helper creation. Optional on helper update. "
                    "Ignored for helper_type='config_subentry', which uses "
                    "entry_id/subentry_type/subentry_id instead. For flow-based "
                    "helper updates (template, group, utility_meter, ...), this is "
                    "ignored because options flows don't expose renaming; change the "
                    "resulting entity's display name with ha_set_entity(name=...)."
                ),
                default=None,
            ),
        ] = None,
        helper_id: Annotated[
            str | None,
            Field(
                description=(
                    "The helper's storage id ('my_button'), not the name of its "
                    "entity, or its full entity_id ('input_button.my_button'). "
                    "Omit to create a new helper."
                ),
                default=None,
            ),
        ] = None,
        entry_id: Annotated[
            str | None,
            Field(
                description=(
                    "Parent config entry ID when helper_type='config_subentry'. "
                    "Use ha_get_integration() to find entry IDs."
                ),
                default=None,
            ),
        ] = None,
        subentry_type: Annotated[
            str | None,
            Field(
                description=(
                    "Integration-defined subentry type when "
                    "helper_type='config_subentry'."
                ),
                default=None,
            ),
        ] = None,
        subentry_id: Annotated[
            str | None,
            Field(
                description=(
                    "Existing config subentry ID to reconfigure when "
                    "helper_type='config_subentry'."
                ),
                default=None,
            ),
        ] = None,
        show_advanced_options: Annotated[
            bool,
            Field(
                description=(
                    "When helper_type='config_subentry', ask older Home "
                    "Assistant versions to expose advanced flow options. No-op "
                    "on HA 2026.6+; pending removal before HA 2027.6."
                ),
                default=False,
            ),
        ] = False,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:bell'); '' or ' ' clears it, except a zone's stored icon (the icon in the zone's own config, as the Home Assistant UI stores it; ha_get_zone shows it), which cannot be removed. On create, a storage helper's icon goes into its stored config when Home Assistant's schema for the type has an icon field, otherwise (person, tag, a new zone, or without the ha_mcp_tools component) into the entity registry; an update always sets the registry icon, and the stored icon too where the schema has one. Automatic backups hold the registry icon of a person, tag or zone only, so restoring another helper's backup does not bring back an icon kept in the registry; without the component, every storage helper other than a zone keeps its icon there",
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description="Area ID for the helper; '' or ' ' clears it", default=None
            ),
        ] = None,
        labels: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(description="Labels to categorize the helper", default=None),
        ] = None,
        # Type fields for SIMPLE helpers, also accepted inside `config`; hidden from
        # the schema so only `config` documents them.
        min_value: Annotated[
            float | None,
            HIDDEN_PARAM,
            Field(
                default=None,
                validation_alias=AliasChoices("min_value", "min", "minimum"),
            ),
        ] = None,
        max_value: Annotated[
            float | None,
            HIDDEN_PARAM,
            Field(
                default=None,
                validation_alias=AliasChoices("max_value", "max", "maximum"),
            ),
        ] = None,
        step: Annotated[float | None, HIDDEN_PARAM] = None,
        unit_of_measurement: Annotated[
            str | None,
            HIDDEN_PARAM,
            Field(
                default=None,
                validation_alias=AliasChoices("unit_of_measurement", "unit"),
            ),
        ] = None,
        options: Annotated[
            str | list[str] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        initial: Annotated[str | bool | int | float | None, HIDDEN_PARAM] = None,
        mode: Annotated[str | None, HIDDEN_PARAM] = None,
        has_date: Annotated[bool | None, HIDDEN_PARAM] = None,
        has_time: Annotated[bool | None, HIDDEN_PARAM] = None,
        restore: Annotated[bool | None, HIDDEN_PARAM] = None,
        duration: Annotated[str | int | float | None, HIDDEN_PARAM] = None,
        monday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        tuesday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        wednesday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        thursday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        friday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        saturday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        sunday: Annotated[
            list[dict[str, Any]] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        latitude: Annotated[float | None, HIDDEN_PARAM] = None,
        longitude: Annotated[float | None, HIDDEN_PARAM] = None,
        radius: Annotated[float | None, HIDDEN_PARAM] = None,
        passive: Annotated[bool | None, HIDDEN_PARAM] = None,
        user_id: Annotated[str | None, HIDDEN_PARAM] = None,
        device_trackers: Annotated[
            list[str] | None, HIDDEN_PARAM, JSON_STRING_COERCION
        ] = None,
        picture: Annotated[str | None, HIDDEN_PARAM] = None,
        tag_id: Annotated[str | None, HIDDEN_PARAM] = None,
        description: Annotated[str | None, HIDDEN_PARAM] = None,
        pattern: Annotated[str | None, HIDDEN_PARAM] = None,
        category: Annotated[
            str | None,
            Field(
                description="Category ID for this helper (ha_config_get_category(scope='helpers') lists them, ha_config_set_category() creates one); '' or ' ' clears it.",
                default=None,
            ),
        ] = None,
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Type-specific fields. On update it is a patch: a field "
                    "you omit keeps its current value. SIMPLE types take "
                    f"these keys: {_SIMPLE_CONFIG_KEYS_DESCRIPTION} "
                    "FLOW types and config_subentry take the flow's fields; a "
                    "field set to null is cleared where the schema allows "
                    "that field to be empty. A field two "
                    "steps declare gets your one value both times; pass "
                    "step_values={'<step_id>': {'<field>': <value>}} to give "
                    "a step its own value, or to leave it out of that step; a "
                    "LIST of those objects supplies one per encounter when the "
                    "flow presents a step more than once."
                ),
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool,
            Field(
                description="Wait for helper entity to be queryable before returning. Set to False "
                "for bulk operations.",
                default=True,
            ),
        ] = True,
        action: Annotated[
            Literal["create", "update"] | None,
            Field(
                description=(
                    "Explicit intent: 'create' a new helper or 'update' an existing one. "
                    "Pass it so a helper_id typo fails as 'helper not found' instead of "
                    "creating a helper."
                ),
                default=None,
            ),
        ] = None,
        MandatoryBPS: Annotated[
            bool,
            Field(default=True),
        ] = True,
        # BestPracticeKey (#1779): consumed by StrictBpsMiddleware, never read
        # here — see strict_bps.py for the declaration contract.
        BestPracticeKey: BestPracticeKeyParam = None,
    ) -> dict[str, Any]:
        """Create or update Home Assistant helpers: storage types, every helper
        flow HA lists, and config subentries.

        MUST call ha_get_skill_guide OR refer to your locally installed skills first.
        ``helper-selection.md`` ships under ``skill_content`` by default.

        SIMPLE types (pass `config` dict): input_boolean, input_button,
        input_select, input_number, input_text, input_datetime, counter, timer, schedule,
        zone, person, tag. Create requires `name`; update requires `helper_id`.

        FLOW types (pass `config` dict, Config Entry Flow API): every helper flow
        Home Assistant lists, read from it at call time — template, group,
        utility_meter, derivative, min_max, threshold, integration, statistics,
        trend, ... and custom helper integrations. Create requires `name`; for
        updates pass the existing entry_id as `helper_id` (options flows reject
        the `name` key).
        Do not create `otp` here although Home Assistant lists it: the user sets
        it up in the HA UI, since its secret is a credential they enroll in an
        authenticator app.

        CONFIG_SUBENTRY type (Config Subentry Flow API): pass `entry_id`,
        `subentry_type` and `config`; pass `subentry_id` to reconfigure an existing
        subentry, omit it to create one.

        Behavior:
        - UPDATE preserves type-specific fields not re-passed (a rename never wipes
          initial/icon/etc.); flow-helper and config subentry updates behave the same
          way (see `config`).
        - Omitted `action` falls back to the `helper_id`-presence discriminator
          (SIMPLE/FLOW) or the `subentry_id`-presence discriminator (config
          subentries).
        - For flow-based helpers, config keys not declared by any step's data_schema
          are silently ignored by HA. Validation errors carry the helper's
          `data_schema` (and `menu_options` for menu-rooted helpers like
          `template`/`group` when no sub-type is chosen yet), so a follow-up call
          can self-correct without a separate schema-discovery round-trip.
        - Flows that present more than one menu (e.g. an MQTT device subentry
          reconfigure looping through its summary menu) take `next_step_id` as a
          LIST of successive selections, consumed one per menu encounter.

        EXAMPLES:
        - input_number: ha_config_set_helper(helper_type="input_number", name="Target", config={"min": 0, "max": 100, "step": 5})
        - template sensor: ha_config_set_helper(helper_type="template", name="Room Temp", config={"next_step_id": "sensor", "state": "{{ states('sensor.x')|float }}", "unit_of_measurement": "°C"})
        - group: ha_config_set_helper(helper_type="group", name="Kitchen Lights", config={"group_type": "light", "entities": ["light.a", "light.b"]})
        - config subentry: ha_config_set_helper(helper_type="config_subentry", entry_id="01HXYZ...", subentry_type="conversation", config={"name": "Local agent", "model": "gemma3:27b"})
        """
        try:
            area_id = clear_or_keep(area_id, "area_id")
            icon = clear_or_keep(icon, "icon")
            category = clear_or_keep(category, "category")
            if helper_type == "config_subentry":
                return await self._set_config_subentry(
                    action,
                    entry_id,
                    subentry_type,
                    subentry_id,
                    show_advanced_options,
                    config,
                    MandatoryBPS,
                )

            await self._require_helper_type(helper_type)
            action = await self._validate_action(action, helper_id, helper_type)  # type: ignore[assignment]
            async with self._schema_context(helper_type, action):
                type_kw: dict[str, Any] = {
                    "options": options,
                    "initial": initial,
                    "min_value": min_value,
                    "max_value": max_value,
                    "step": step,
                    "unit_of_measurement": unit_of_measurement,
                    "mode": mode,
                    "has_date": has_date,
                    "has_time": has_time,
                    "restore": restore,
                    "duration": duration,
                    "monday": monday,
                    "tuesday": tuesday,
                    "wednesday": wednesday,
                    "thursday": thursday,
                    "friday": friday,
                    "saturday": saturday,
                    "sunday": sunday,
                    "latitude": latitude,
                    "longitude": longitude,
                    "radius": radius,
                    "passive": passive,
                    "user_id": user_id,
                    "device_trackers": device_trackers,
                    "picture": picture,
                    "tag_id": tag_id,
                    "description": description,
                    "pattern": pattern,
                }
                name, icon, type_kw, passthrough = self._prepare_typed_params(
                    helper_type, config, name, icon, type_kw
                )

                # Bug 12: detect name collision before sending so the caller isn't silently given a duplicate.
                if action == "create":
                    await self._check_name_collision(helper_type, name)

                if helper_type not in SIMPLE_HELPER_TYPES:
                    flow_response = await self._flow_helper(
                        helper_type,
                        name,
                        helper_id,
                        config,
                        area_id,
                        labels,
                        category,
                        wait,
                        icon=icon,
                        action=action,
                    )
                    await self._attach_helper_skill(flow_response, MandatoryBPS)
                    return flow_response

                try:
                    labels = parse_string_list_param(labels, "labels")
                    type_kw["options"] = parse_string_list_param(
                        type_kw["options"], "options"
                    )
                except ValueError as e:
                    raise_failure(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"Invalid list parameter: {e}",
                        )
                    )

                # Bug 16 (issue #1150): validate area_id / labels / category exist.
                await self._validate_registry_ids(
                    area_id,
                    labels,
                    {"helpers": category},
                    fail_closed=True,
                )

                # Home Assistant validates the fields itself (#2632).
                fields = core_fields(helper_type, type_kw, passthrough)

                if action == "create":
                    return await self._create_simple(
                        helper_type,
                        name,
                        icon,
                        area_id,
                        labels,
                        category,
                        wait,
                        MandatoryBPS,
                        fields,
                    )

                if action != "update":
                    raise_failure(
                        create_error_response(
                            ErrorCode.INTERNAL_ERROR,
                            f"Unexpected action: {action}",
                        )
                    )

                # helper_id is guaranteed non-None by self._validate_action for update
                hid: str = helper_id  # type: ignore[assignment]
                # A bare id is a storage id, never turned into an entity_id.
                return await self._update_simple(
                    helper_type,
                    hid,
                    hid,
                    name,
                    icon,
                    area_id,
                    labels,
                    category,
                    wait,
                    MandatoryBPS,
                    fields,
                )

        except Exception as error:
            _LOGGER.debug("Mapping helper failure", exc_info=True)
            return self._map_helper_error(error, action, helper_type)
