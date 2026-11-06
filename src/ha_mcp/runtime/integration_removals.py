"""Shared deletion routing with explicit Core/remote removal operations."""

from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp.errors import ErrorCode, HomeAssistantAPIError, create_error_response
from ha_mcp.tool_api import ToolFailure

from .errors import raise_failure
from .flows.entry import refuse_component_entry
from .flows.session import FlowSession
from .helper_removal_entries import EntryRemovalTools
from .helper_schema import SIMPLE_HELPER_TYPES, StorageHelperType
from .helper_targets import resolve_helper_entity
from .validation import validate_identifier_not_empty

HelperTypeLiteral = StorageHelperType | Literal["config_subentry"] | str


class IntegrationRemovalTools(EntryRemovalTools):
    async def ha_remove_helpers_integrations(
        self,
        target: Annotated[
            str,
            Field(
                description=(
                    "What to remove. One of: "
                    "(a) bare helper_id for SIMPLE helpers: the helper's "
                    "storage id, not the name of its entity, e.g. 'my_button'; "
                    "(b) full entity_id, "
                    "e.g. 'input_button.my_button' or 'sensor.my_meter'; "
                    "(c) config entry_id for any integration, with helper_type "
                    "omitted, e.g. value from ha_get_integration(); "
                    "(d) parent config entry_id for a config subentry."
                )
            ),
        ],
        helper_type: Annotated[
            HelperTypeLiteral | None,
            Field(
                description=(
                    "Helper type of target; ROUTING says which value takes "
                    "which removal path."
                ),
                default=None,
            ),
        ] = None,
        subentry_id: Annotated[
            str | None,
            Field(
                description=(
                    "Config subentry ID to remove when helper_type='config_subentry'."
                ),
                default=None,
            ),
        ] = None,
        confirm: Annotated[
            bool,
            Field(
                description="Must be True to confirm removal.",
                default=False,
            ),
        ] = False,
        wait: Annotated[
            bool,
            Field(
                description=(
                    "Wait for entity removal. Default: True. "
                    "Ignored for a direct config entry delete (an entry_id "
                    "target) or helper_type='config_subentry' (no entity "
                    "poll, require_restart returned)."
                ),
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Remove a Home Assistant helper or integration config entry.

        Unifies three backend removal mechanisms — simple-helper websocket
        delete, config-entry delete, and config-subentry delete — behind one
        entry point; helper_type picks the path, or the entity registry does
        when an entity_id comes without one.

        WHEN NOT TO USE:
        - Removing only an entity (without deleting its underlying helper or
          config entry) — use `ha_remove_entity` instead.
        - YAML-configured helpers — they have no storage backend. Edit the
          YAML file and reload the relevant integration.
        - ha-mcp's own ha_mcp_tools entries are refused: manage them in the HA
          UI, or (server settings, developer mode on) ha_dev_manage_settings /
          ha_dev_manage_server.

        ROUTING:
        - SIMPLE helper_type (input_button, input_boolean, input_select,
          input_number, input_text, input_datetime, counter, timer, schedule,
          zone, person, tag) + bare helper_id (its storage id) or entity_id →
          websocket delete.
        - FLOW helper_type (any helper flow Home Assistant lists: template, group,
          utility_meter, derivative, ...; custom helper integrations too)
          + full entity_id → resolve entity_id to
          config_entry_id via entity_registry, then delete the config entry. All
          sub-entities (e.g. utility_meter tariffs) are removed together. An
          entity registered by another integration is refused with
          VALIDATION_INVALID_PARAMETER and nothing is deleted.
        - helper_type=None + entity_id → the entity's registry entry names its
          helper (storage type or helper flow); an entity of any other
          integration is refused with VALIDATION_INVALID_PARAMETER.
        - helper_type=None + entry_id → direct config entry delete (any
          integration).
        - helper_type="config_subentry" + parent entry_id + subentry_id →
          delete one config subentry.

        A target that cannot be removed raises a structured error rather than
        returning silent success: ENTITY_NOT_FOUND when the entity_id is absent
        from both the state machine and the entity registry, or for a bare
        helper_id no stored helper has (a bare helper_id on a FLOW target also
        raises it — FLOW resolution needs a full entity_id); RESOURCE_NOT_FOUND
        when it exists but is not storage- or registry-managed (a YAML-configured helper, or an entity with a state
        but no registry entry), for a config entry the backend reports as 404,
        or for a missing config subentry.
        Calling N times gives the same response. Transient connectivity failures
        raise their own codes (WEBSOCKET_DISCONNECTED, CONNECTION_FAILED) so
        retry logic can branch separately.

        EXAMPLES:
        - Remove SIMPLE button: ha_remove_helpers_integrations(target="my_button", helper_type="input_button", confirm=True)
        - Remove FLOW utility_meter (any sub-entity works): ha_remove_helpers_integrations(target="sensor.energy_peak", helper_type="utility_meter", confirm=True)
        - Remove whatever helper owns an entity: ha_remove_helpers_integrations(target="sensor.energy_peak", confirm=True)
        - Remove any integration by entry_id: ha_remove_helpers_integrations(target="01HXYZ...", confirm=True)
        - Remove a config subentry: ha_remove_helpers_integrations(target="01HXYZ...", helper_type="config_subentry", subentry_id="subentry-123", confirm=True)

        WARNING: Removing a helper or integration that is referenced by
        automations, scripts, or other integrations may cause those to fail.
        Use ha_search() / ha_get_integration() to verify before removal.
        ha_manage_backup(scope="edits", action="restore") recreates removed FLOW
        helpers whose options could be snapshotted, and config subentries. It
        cannot recreate custom helper integrations (their options are withheld)
        or otp (no stored options); re-add those yourself, otp through the user
        in the HA UI.
        """
        # === Confirm gate (uniform for every path) ===
        if not confirm:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Deletion not confirmed. Set confirm=True to proceed.",
                    context={
                        "target": target,
                        "helper_type": helper_type,
                        "warning": (
                            "This will delete the helper or integration. "
                            "Recovery is not guaranteed."
                        ),
                    },
                )
            )

        # === Empty/whitespace target gate (uniform for every path) ===
        # Empty/whitespace ``target`` would reach the destructive backend call
        # on every path: Path 1 (simple-helper websocket delete), Path 2
        # (flow-helper entity-resolution → entry_id delete), Path 3
        # (_delete_direct_entry → client.delete_config_entry("")), Path 4
        # (_delete_config_subentry → ws delete on empty parent entry_id).
        # Each path surfaces a different misleading error from HA. Reject
        # up-front so the caller learns the identifier was unusable before
        # any backend call.
        validate_identifier_not_empty(
            target,
            "target",
            suggestions=[
                "Use ha_get_integration() to find valid entry_ids",
                "For simple helpers, use ha_search() to find the helper_id",
                "For flow helpers, use ha_search() to find an entity_id",
            ],
            context={"helper_type": helper_type},
        )

        wait_bool = wait
        warnings: list[str] = []

        # === Routing dispatch ===
        if helper_type is None and "." in target:
            # An entity_id alone: its registry entry names the helper, and the
            # resolved call runs the matching path with its own backup.
            resolved_type, resolved_target = await self._resolve_helper_entity(target)
            resolved: dict[str, Any] = await self._remove_resolved_helper(
                target=resolved_target, helper_type=resolved_type, wait=wait_bool
            )
            return resolved

        if helper_type is None:
            # Path 3: Direct config entry delete (any integration)
            return await self._delete_direct_entry(target)

        if helper_type == "config_subentry":
            # Path 4: Delete one subentry under a parent config entry
            subentry_id = validate_identifier_not_empty(
                subentry_id,
                "subentry_id",
                suggestions=[
                    "Use ha_get_integration(entry_id=..., include_subentries=True) "
                    "to find subentry IDs",
                ],
                context={"target": target, "helper_type": helper_type},
            )
            return await self._delete_config_subentry(target, subentry_id)

        if helper_type in SIMPLE_HELPER_TYPES:
            # Path 1: SIMPLE helper via websocket delete
            return await self._delete_simple_helper(helper_type, target, wait_bool)

        # Path 2: FLOW helper via entity_id → config_entry_id lookup; a type
        # Home Assistant does not list as a helper flow is refused first.
        await self._require_remove_type(helper_type)
        return await self._delete_flow_helper(helper_type, target, wait_bool, warnings)

    async def _delete_direct_entry(self, entry_id: str) -> dict[str, Any]:
        """Delete a config entry directly via the REST delete API.

        Looks the entry up first and refuses ha-mcp's own ``ha_mcp_tools``
        entries before any DELETE is sent.
        """
        try:
            entry = await self._read_remove_entry(entry_id)
            refuse_component_entry(
                entry.get("domain"),
                action="delete a config entry of",
                entry_id=entry_id,
            )
            result = await self._remove_entry(entry_id, entry)
            require_restart = result.get("require_restart", False)
            return {
                "success": True,
                "action": "delete",
                "target": entry_id,
                "helper_type": "config_entry",
                "method": "config_entry_delete",
                "entry_id": entry_id,
                "entity_ids": [],
                "require_restart": require_restart,
                **({"warnings": result["warnings"]} if result.get("warnings") else {}),
                "message": (
                    "Config entry deleted successfully."
                    if not require_restart
                    else "Config entry deleted; Home Assistant restart required."
                ),
            }
        except ToolFailure:
            raise
        except HomeAssistantAPIError as e:
            # A missing entry surfaces as HomeAssistantAPIError with
            # status_code=404 from the own-entry lookup
            # (RestClient.get_config_entry), before any DELETE is sent, or
            # from the DELETE itself when the entry vanished in between
            # (RestClient.delete_config_entry). Surface it as
            # RESOURCE_NOT_FOUND so callers can distinguish an absent target
            # from a real failure; the typo case (agent passed the wrong
            # entry_id) is the failure mode that "absent → success" would
            # silently mask. Non-404 API errors are real failures and bubble
            # through exception_to_structured_error below.
            if e.status_code == 404:
                raise_failure(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        (
                            f"Config entry {entry_id} not found. May "
                            "indicate it was already removed, never "
                            "existed, or the identifier is a typo. "
                            "Verify with ha_get_integration() before "
                            "retrying."
                        ),
                        context={"entry_id": entry_id},
                        suggestions=[
                            "Use ha_get_integration() without entry_id "
                            "to see all config entries",
                        ],
                    )
                )
            self._classify_removal_error(
                e,
                context={"entry_id": entry_id},
                suggestions=[
                    "Use ha_get_integration() without entry_id to "
                    "see all config entries",
                ],
            )
        except Exception as e:  # noqa: BLE001
            self._classify_removal_error(
                e,
                context={"entry_id": entry_id},
                suggestions=[
                    "Use ha_get_integration() without entry_id to "
                    "see all config entries",
                ],
            )
            return None  # unreachable: exception_to_structured_error raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    async def _require_remove_type(self, helper_type: str) -> None:
        from .flows.helpers import _raise_unknown_helper_type

        types = await self.backend.execute(
            "helpers.flow.types", {"helper_type": helper_type}, self.context
        )
        if helper_type not in types["types"]:
            _raise_unknown_helper_type(helper_type, frozenset(types["types"]))

    async def _resolve_helper_entity(
        self, entity_id: str
    ) -> tuple[HelperTypeLiteral, str]:
        return await resolve_helper_entity(
            FlowSession(self.backend, self.context), entity_id
        )

    async def _remove_resolved_helper(
        self, *, target: str, helper_type: HelperTypeLiteral, wait: bool
    ) -> dict[str, Any]:
        if helper_type in SIMPLE_HELPER_TYPES:
            return await self._delete_simple_helper(helper_type, target, wait)
        return await self._delete_flow_helper(helper_type, target, wait, [])
