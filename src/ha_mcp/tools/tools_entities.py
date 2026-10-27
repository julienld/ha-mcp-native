"""
Entity management tools for Home Assistant MCP server.

This module provides tools for managing entity lifecycle and properties
via the Home Assistant entity registry API.
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any, Literal, cast

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import LegacyBackendTools, legacy_errors, legacy_errors_sync
from ..backends.legacy_entity_snapshots import (
    capture_entity_device,
    capture_entity_registry,
)
from ..backends.legacy_registry_errors import raise_entity_read_error
from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from ..runtime import entity_write_values as _write_values
from ..runtime.entities import EntityDiscoveryTools
from ..runtime.entity_records import _format_fetched_entity
from ..runtime.entity_removal_tools import EntityRemovals, EntityRemovalTools
from ..runtime.entity_write_tools import EntityWrites, EntityWriteTools
from ..runtime.entity_write_values import apply_entity_rename
from ..utils.registry_update_lock import registry_update_lock
from .auto_backup import with_auto_backup
from .component_api import (
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .config_helpers.registry import validate_registry_ids
from .entity_param_parsing import (
    _parse_aliases_param,
    _parse_categories_param,
    _parse_expose_to_param,
    _parse_options_param,
    _parse_string_list_field,
)
from .entity_removal import remove_registry_entry
from .entity_update_fields import (
    build_name_visibility_fields,
    build_state_tag_fields,
    ensure_use_device_name_allowed,
    reject_name_with_use_device_name,
)
from .helpers import (
    clearable_value,
    exception_to_structured_error,
    extract_tool_error_message,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)

# The ha_mcp_tools/entity_enrich WS command: resolved area/floor/label NAMES +
# aliases per entity_id, byte-shaped like the component's registry join. Named
# once so the routing helper and its tests stay in lockstep.
WS_ENTITY_ENRICH = "ha_mcp_tools/entity_enrich"

# Bounds the per-frame size of a bulk config/entity_registry/get_entries call
# (extended entries, ~1KB each) so a large id list can't produce an over-cap
# WebSocket frame. Matches the chunk size smart_search uses for the same
# command. Typical bulk calls fit in a single chunk (one WS message).
_GET_ENTRIES_CHUNK_SIZE = 500

# Max entity IDs accepted by the bulk-removal path of ha_remove_entity. Mirrors
# the cap the other bulk tools use (ha_get_state's _get_bulk_entity_states).
_MAX_BULK_REMOVE = 100

_CLEAR_DEVICE_NAME_HINT = (
    "new_device_name cannot clear the device name; use ha_set_device(name='') "
    "instead, or name=' ' if your client cannot send an empty string"
)


async def fetch_entity_enrichment_via_component(
    client: Any, entity_ids: list[str]
) -> dict[str, dict[str, Any]] | None:
    """One ``ha_mcp_tools/entity_enrich`` read; ``None`` ⇒ skip enrichment (legacy).

    Returns the component's ``{id: {area, floor, labels, aliases}}`` map (resolved
    area/floor/label NAMES the raw registry entry lacks — it carries ``area_id`` /
    label *ids*) or ``None`` when the component lacks the ``entity_enrich``
    capability, was downgraded (``unknown_command`` → invalidate the cached caps),
    or errored (logged). ``None`` means "no enrichment available", NOT "no such
    entity": ``ha_get_entity``'s registry read already served the base record, so a
    ``None`` here simply leaves the additive fields off — the enrichment is
    strictly additive, so its absence changes nothing else. A
    ``HomeAssistantConnectionError`` — a pooled-WS drop, or a failed
    (re)connect — is caught here and mapped to ``None``: there is no legacy enrichment fetch (the base record is
    already served), so a transport failure just skips enrichment rather than
    escaping into and failing the whole ``ha_get_entity``. Follows the same
    caps-gate discipline as ``component_devices.fetch_device_via_component``.

    The ids are split into ``_GET_ENTRIES_CHUNK_SIZE`` chunks (the same bound the
    sibling ``config/entity_registry/get_entries`` read uses) so a large bulk
    request cannot produce an over-cap WebSocket frame or stall in the optional
    enrichment step. Enrichment stays all-or-nothing: any chunk failing or drifting
    in shape returns ``None`` (legacy shape), never a partially-enriched response.
    """
    if not entity_ids:
        return None
    caps = await get_component_caps(client)
    if not (
        component_supports(caps, "entity_enrich")
        and component_supports(caps, DEVICE_REGISTRY_CHILD_SEMANTICS)
    ):
        return None
    ids = list(entity_ids)
    chunks = [
        ids[i : i + _GET_ENTRIES_CHUNK_SIZE]
        for i in range(0, len(ids), _GET_ENTRIES_CHUNK_SIZE)
    ]
    merged: dict[str, dict[str, Any]] = {}
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        for chunk in chunks:
            raw = await ws.send_command(WS_ENTITY_ENRICH, entity_ids=chunk)
            result = raw.get("result")
            if not isinstance(result, dict):
                return None
            entities = result.get("entities")
            if not isinstance(entities, dict):
                return None
            merged.update(entities)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; skipped enrichment: %r", WS_ENTITY_ENRICH, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        # HomeAssistantConnectionError / plain establish Exception → skip
        # enrichment (strictly additive; no legacy fetch dies here).
        logger.warning(
            "%s connection error; skipped enrichment: %r", WS_ENTITY_ENRICH, exc
        )
        return None
    return merged


_format_entity_entry = _write_values._format_entity_entry


def _extract_ws_error(result: dict[str, Any]) -> str:
    """Pull a user-readable message out of a failed WebSocket response.

    Falls back to a static placeholder + warning log when HA returns an
    empty or malformed error envelope, so the user-facing message never
    degrades to literal "{}".
    """
    error = result.get("error")
    if isinstance(error, dict):
        msg = error.get("message")
        if isinstance(msg, str) and msg:
            return msg
    elif isinstance(error, str) and error:
        return error
    logger.warning("HA WS response had no usable error detail: %r", result)
    return "no error detail returned by Home Assistant"


_parse_set_entity_ids = legacy_errors_sync(_write_values._parse_set_entity_ids)


_validate_enabled_constraint = legacy_errors_sync(
    _write_values._validate_enabled_constraint
)


class EntityTools(LegacyBackendTools, EntityDiscoveryTools):
    """Entity registry tools: get, update, and remove entities."""

    @property
    def entities(self) -> EntityWrites:
        # The public façade forwards only its declared keyword arguments.
        return cast(EntityWrites, self)

    ha_set_entity = tool(
        name="ha_set_entity",
        tags={"Entity Registry"},
        annotations=write_hints(
            "Set Entity", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(
            domain="entity_registry", id_param="entity_id", skip_fn=lambda kw: True
        )(log_tool_usage(legacy_errors(EntityWriteTools.ha_set_entity)))
    )

    @property
    def removals(self) -> EntityRemovals:
        return self

    ha_remove_entity = tool(
        name="ha_remove_entity",
        tags={"Entity Registry"},
        annotations=write_hints(
            "Remove Entity", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(
            domain="entity_registry", id_param="entity_id", skip_fn=lambda kw: True
        )(log_tool_usage(legacy_errors(EntityRemovalTools.ha_remove_entity)))
    )

    def __init__(self, client: Any) -> None:
        super().__init__(client)

    async def _get_entity_labels(
        self, entity_id: str
    ) -> tuple[list[str] | None, str | None]:
        """Fetch current labels for an entity. Returns (labels, error_msg)."""
        get_msg: dict[str, Any] = {
            "type": "config/entity_registry/get",
            "entity_id": entity_id,
        }
        result = await self._client.send_websocket_message(get_msg)
        if not result.get("success"):
            return None, _extract_ws_error(result)
        return (result.get("result") or {}).get("labels") or [], None

    async def _get_entity_aliases(self, entity_id: str) -> list[str | None]:
        """Fetch the entity's current registry aliases (``null`` = own name)."""
        get_msg: dict[str, Any] = {
            "type": "config/entity_registry/get",
            "entity_id": entity_id,
        }
        result = await self._client.send_websocket_message(get_msg)
        if not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to get current aliases for {entity_id}: "
                    f"{_extract_ws_error(result)}",
                    context={"entity_id": entity_id},
                )
            )
        aliases: list[str | None] = (result.get("result") or {}).get("aliases") or []
        return aliases

    async def _resolve_final_aliases(
        self,
        entity_id: str,
        parsed_aliases: list[str | None] | None,
        use_entity_name_alias: bool | None,
    ) -> tuple[list[str | None] | None, str | None]:
        need_current = (
            use_entity_name_alias is None
            and parsed_aliases is not None
            and None not in parsed_aliases
        ) or (use_entity_name_alias is not None and parsed_aliases is None)
        current = await self._get_entity_aliases(entity_id) if need_current else []
        return _write_values.merge_entity_aliases(
            current, parsed_aliases, use_entity_name_alias
        )

    async def _resolve_final_labels(
        self,
        entity_id: str,
        parsed_labels: list[str] | None,
        label_operation: str,
    ) -> list[str] | None:
        """Merge or filter labels using the current registry state for add/remove ops."""
        if parsed_labels is None or label_operation not in ("add", "remove"):
            return parsed_labels
        current_labels, error_msg = await self._get_entity_labels(entity_id)
        if current_labels is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to get current labels for {entity_id}: {error_msg}",
                    context={"entity_id": entity_id},
                )
            )
        return _write_values.merge_entity_labels(
            current_labels, parsed_labels, label_operation
        )

    _validate_entity_rename = staticmethod(legacy_errors_sync(apply_entity_rename))

    async def _execute_registry_update(
        self,
        entity_id: str,
        message: dict[str, Any],
        updates_made: list[str],
        new_entity_id: str | None,
    ) -> tuple[str, dict[str, Any], bool]:
        """Send entity registry update. Returns (effective_entity_id, entity_entry, has_registry_updates)."""
        has_registry_updates = len(message) > 2  # more than just type + entity_id
        entity_entry: dict[str, Any] = {}

        if has_registry_updates:
            logger.info(
                f"Updating entity registry for {entity_id}: {', '.join(updates_made)}"
            )
            result = await self._client.send_websocket_message(message)

            if not result.get("success"):
                error_msg = _extract_ws_error(result)
                suggestions = ["Verify the entity_id exists using ha_search()"]
                if new_entity_id is not None:
                    suggestions.extend(
                        [
                            "Check that the new entity_id doesn't already exist",
                            "Ensure the entity has a unique_id (some legacy entities cannot be renamed)",
                        ]
                    )
                else:
                    suggestions.extend(
                        [
                            "Check that area_id exists if specified",
                            "Some entities may not support all update options",
                        ]
                    )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to update entity: {error_msg}",
                        context={"entity_id": entity_id},
                        suggestions=suggestions,
                    )
                )

            entity_entry = result.get("result", {}).get("entity_entry", {})

            # If entity was renamed, update entity_id for subsequent operations
            if new_entity_id:
                entity_id = new_entity_id

        return entity_id, entity_entry, has_registry_updates

    async def _apply_options_updates(
        self,
        entity_id: str,
        parsed_options: dict[str, dict[str, Any]] | None,
        entity_entry: dict[str, Any],
        has_registry_updates: bool,
        updates_made: list[str],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Apply per-domain options updates. Returns (entity_entry, options_succeeded).

        Per-domain options updates: HA's WS schema requires `options_domain`
        and `options` to be sent paired one domain per call (the API takes a
        single domain's sub-dict). An agent-supplied {domain: {...}, ...} is
        therefore split into one registry update per domain.
        """
        options_succeeded: dict[str, dict[str, Any]] = {}
        if not parsed_options:
            return entity_entry, options_succeeded

        for opts_domain, opts_sub in parsed_options.items():
            opts_msg: dict[str, Any] = {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "options_domain": opts_domain,
                "options": opts_sub,
            }
            opts_result = await self._client.send_websocket_message(opts_msg)
            if not opts_result.get("success"):
                err_msg = _extract_ws_error(opts_result)
                partial = bool(options_succeeded) or has_registry_updates
                msg_prefix = (
                    "Partially updated entity; failed updating options for"
                    if partial
                    else "Failed to update options for"
                )
                # `options_succeeded` is the structured retriable form
                # (agent can re-feed it minus the failing domain).
                # `updates_applied` is the human-readable prose list
                # including non-options updates (name=, icon=, etc.).
                # Both are surfaced — they serve different consumers.
                options_failure_context: dict[str, Any] = {
                    "entity_id": entity_id,
                    "options_domain": opts_domain,
                    "partial": partial,
                    "options_succeeded": options_succeeded,
                    "updates_applied": list(updates_made),
                }
                # Only include entity_entry when something actually mutated;
                # _format_entity_entry({}) returns an all-None stub that's
                # indistinguishable from "entity has nothing set". Mirrors
                # the partial-context logic in _build_expose_failure_context.
                if partial:
                    options_failure_context["entity_entry"] = _format_entity_entry(
                        entity_entry
                    )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"{msg_prefix} domain '{opts_domain}': {err_msg}",
                        context=options_failure_context,
                    )
                )
            # HA returns the cumulative entity_entry on each per-domain
            # call, so last-call-wins reassignment leaves the final loop
            # iteration carrying the full state.
            entity_entry = (opts_result.get("result") or {}).get(
                "entity_entry", entity_entry
            )
            options_succeeded[opts_domain] = opts_sub
            updates_made.append(f"options[{opts_domain}]={opts_sub}")

        return entity_entry, options_succeeded

    async def _apply_device_rename(
        self,
        entity_id: str,
        entity_entry: dict[str, Any],
        new_device_name: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Rename the associated device. Returns (device_rename_result, entity_entry).

        ``new_device_name`` is already normalized: None means no rename.
        """
        device_rename_result: dict[str, Any] | None = None
        if new_device_name is None:
            return device_rename_result, entity_entry

        # If no registry update was sent, fetch entity_entry to get device_id
        if not entity_entry:
            device_lookup_msg: dict[str, Any] = {
                "type": "config/entity_registry/get",
                "entity_id": entity_id,
            }
            get_result = await self._client.send_websocket_message(device_lookup_msg)
            if get_result.get("success"):
                entity_entry = get_result.get("result") or {}
            else:
                logger.warning(
                    "Entity registry lookup failed for %s: %s",
                    entity_id,
                    _extract_ws_error(get_result),
                )
                device_rename_result = {
                    "warnings": [
                        "Entity registry lookup failed — could not determine device. Retry may succeed."
                    ],
                    "lookup_failed": True,
                }

        device_id = entity_entry.get("device_id") if not device_rename_result else None
        if not device_id:
            # Only fire the "no device" warning when the registry lookup
            # succeeded — otherwise the "lookup failed" warning set above
            # already carries the more accurate signal, and a second
            # "no associated device" claim would be unverified (we don't
            # actually know what the registry says when the lookup failed).
            if device_rename_result is None:
                device_rename_result = {
                    "warnings": [
                        "Entity has no associated device — device rename skipped"
                    ],
                }
            return device_rename_result, entity_entry

        device_msg: dict[str, Any] = {
            "type": "config/device_registry/update",
            "device_id": device_id,
            "name_by_user": new_device_name,
        }
        await capture_entity_device(self._client, device_id)
        device_result = await self._client.send_websocket_message(device_msg)
        if device_result.get("success"):
            device_rename_result = {"success": True, "device_id": device_id}
        else:
            device_rename_result = {
                "warnings": [
                    f"Entity updated but device rename failed: {_extract_ws_error(device_result)}"
                ],
                "device_id": device_id,
            }
        return device_rename_result, entity_entry

    def _build_expose_failure_context(
        self,
        entity_id: str,
        entity_entry: dict[str, Any],
        succeeded: dict[str, bool],
        failed: dict[str, bool],
        options_succeeded: dict[str, dict[str, Any]],
        device_rename_result: dict[str, Any] | None,
        has_registry_updates: bool,
    ) -> dict[str, Any]:
        """Build error context for an expose_to failure.

        `partial` must reflect every prior mutation across the ha_set_entity
        pipeline: main registry update, per-domain options, device rename, and
        any expose_to batch (e.g. expose_true) that ran before this
        one (expose_false) failed. Anything truthy in those means
        the registry already moved.
        """
        prior_mutation = (
            has_registry_updates
            or bool(options_succeeded)
            or bool(succeeded)
            or bool(device_rename_result and device_rename_result.get("success"))
        )
        context: dict[str, Any] = {
            "entity_id": entity_id,
            "exposure_succeeded": succeeded,
            "exposure_failed": failed,
        }
        if prior_mutation:
            context["partial"] = True
            context["entity_entry"] = _format_entity_entry(entity_entry)
            if options_succeeded:
                context["options_succeeded"] = options_succeeded
            if device_rename_result and device_rename_result.get("success"):
                context["device_rename_succeeded"] = True
        return context

    async def _apply_expose_to(
        self,
        entity_id: str,
        parsed_expose_to: dict[str, bool] | None,
        entity_entry: dict[str, Any],
        has_registry_updates: bool,
        options_succeeded: dict[str, dict[str, Any]],
        device_rename_result: dict[str, Any] | None,
    ) -> tuple[dict[str, bool] | None, dict[str, Any]]:
        """Apply expose_to changes via separate WebSocket API. Returns (exposure_result, entity_entry)."""
        if not parsed_expose_to:
            return None, entity_entry

        # Group by should_expose value for efficient API calls
        expose_true = [a for a, v in parsed_expose_to.items() if v]
        expose_false = [a for a, v in parsed_expose_to.items() if not v]
        succeeded: dict[str, bool] = {}

        for assistants, should_expose in [(expose_true, True), (expose_false, False)]:
            if not assistants:
                continue

            expose_msg: dict[str, Any] = {
                "type": "homeassistant/expose_entity",
                "assistants": assistants,
                "entity_ids": [entity_id],
                "should_expose": should_expose,
            }
            logger.info(
                f"{'Exposing' if should_expose else 'Hiding'} {entity_id} "
                f"{'to' if should_expose else 'from'} {assistants}"
            )
            expose_result = await self._client.send_websocket_message(expose_msg)

            if not expose_result.get("success"):
                error_msg = _extract_ws_error(expose_result)
                failed = dict.fromkeys(assistants, should_expose)
                context = self._build_expose_failure_context(
                    entity_id,
                    entity_entry,
                    succeeded,
                    failed,
                    options_succeeded,
                    device_rename_result,
                    has_registry_updates,
                )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Exposure failed: {error_msg}",
                        context=context,
                        suggestions=[
                            "Check Home Assistant connection and entity availability"
                        ],
                    )
                )

            # Track successful exposures
            for a in assistants:
                succeeded[a] = should_expose

        exposure_result: dict[str, bool] | None = succeeded if succeeded else None

        # Exposure mutates the registry entry's options, so refetch to return the
        # post-exposure state. This is unconditional: the method returns early on
        # an empty expose_to (line 695) and any exposure failure already raised
        # above, so reaching here always means at least one exposure was applied.
        get_msg: dict[str, Any] = {
            "type": "config/entity_registry/get",
            "entity_id": entity_id,
        }
        get_result = await self._client.send_websocket_message(get_msg)
        if get_result.get("success"):
            entity_entry = get_result.get("result") or {}
        elif entity_entry:
            # The exposure already committed; only the cosmetic post-exposure
            # refresh failed. Keep the pre-exposure snapshot and warn rather
            # than reporting the whole (successful) operation as a failure.
            logger.warning(
                f"Exposure applied to {entity_id} but its registry state could "
                f"not be refreshed: {_extract_ws_error(get_result)}"
            )
        else:
            # No prior-phase snapshot to fall back on -- surface the read
            # failure with the actual WebSocket error.
            raise_tool_error(
                create_error_response(
                    ErrorCode.ENTITY_NOT_FOUND,
                    f"Entity '{entity_id}' could not be read after applying "
                    f"exposure changes: {_extract_ws_error(get_result)}",
                    context={
                        "entity_id": entity_id,
                        "exposure_succeeded": exposure_result,
                        "has_registry_updates": has_registry_updates,
                        "options_succeeded": options_succeeded,
                    },
                    suggestions=[
                        "Verify the entity_id exists using ha_search()",
                        "The entity's exposure settings were likely changed, but its current state could not be confirmed.",
                    ],
                )
            )

        return exposure_result, entity_entry

    _build_entity_response = staticmethod(_write_values.entity_write_result)

    async def _update_single_entity(
        self,
        entity_id: str,
        area_id: str | None,
        name: str | None,
        icon: str | None,
        enabled: bool | None,
        hidden: bool | None,
        parsed_aliases: list[str | None] | None,
        parsed_categories: dict[str, str | None] | None,
        parsed_labels: list[str] | None,
        label_operation: str,
        parsed_expose_to: dict[str, bool] | None,
        new_entity_id: str | None = None,
        new_device_name: str | None = None,
        device_class: str | None = None,
        parsed_options: dict[str, dict[str, Any]] | None = None,
        use_entity_name_alias: bool | None = None,
        use_device_name: bool | None = None,
    ) -> dict[str, Any]:
        """Update a single entity. Orchestrates the phase pipeline."""
        reject_name_with_use_device_name(name, use_device_name)
        await ensure_use_device_name_allowed(
            use_device_name, self._client, entity_id, self._fetch_entity
        )
        # Normalized before Phase 3: a quote-only value must be rejected before the
        # entity registry write, and a blank one must not count as deferred work.
        device_name_blank = new_device_name is not None and not new_device_name.strip()
        new_device_name = clearable_value(
            new_device_name, "new_device_name", hint=_CLEAR_DEVICE_NAME_HINT
        )
        async with registry_update_lock("entity", entity_id):
            # Phase 1: For add/remove label operations, fetch current labels first
            final_labels = await self._resolve_final_labels(
                entity_id, parsed_labels, label_operation
            )
            parsed_aliases, alias_note = await self._resolve_final_aliases(
                entity_id, parsed_aliases, use_entity_name_alias
            )

            # Phase 2: Build update message for entity registry
            message: dict[str, Any] = {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
            }
            updates_made: list[str] = []
            build_name_visibility_fields(
                message,
                updates_made,
                area_id,
                name,
                icon,
                device_class,
                use_device_name,
            )
            build_state_tag_fields(
                message,
                updates_made,
                enabled,
                hidden,
                parsed_aliases,
                parsed_categories,
                final_labels,
                label_operation,
                parsed_labels,
            )
            if alias_note:
                updates_made.append(alias_note)
            if new_entity_id is not None:
                self._validate_entity_rename(
                    entity_id, new_entity_id, message, updates_made
                )
            # expose_to and device_name are appended to updates_made only after their
            # WS phases run (Phases 5-6), so the Phase-4 error context never claims
            # they were applied before they ran.
            has_deferred_work = (
                parsed_expose_to is not None or new_device_name is not None
            )
            if not updates_made and not parsed_options and not has_deferred_work:
                if device_name_blank:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "new_device_name is blank and no other update was given",
                            suggestions=[_CLEAR_DEVICE_NAME_HINT],
                            context={"entity_id": entity_id},
                        )
                    )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "No updates specified",
                        suggestions=[
                            "Provide at least one of: area_id, name, use_device_name, icon, device_class, enabled, hidden, aliases, use_entity_name_alias, categories, labels, options, expose_to, new_entity_id, or new_device_name"
                        ],
                    )
                )

            # Save original entity_id before potential rename
            original_entity_id = entity_id

            # Issue #2159: validate cross-registry references immediately before
            # the write — the narrowest window against a concurrent registry
            # deletion (#2160 placed the area check here for the same reason).
            # Bulk calls also revalidate here after waiting for the entity lock.
            # For label add, only the added IDs are checked: the merged
            # set may legitimately carry pre-existing dangling labels, whose
            # cleanup path (label_operation="remove") must stay open.
            await validate_registry_ids(
                self._client,
                message.get("area_id"),  # normalized by build_name_visibility_fields
                parsed_labels if label_operation in ("set", "add") else None,
                parsed_categories,
                fail_closed=True,
            )

            if len(message) > 2 or parsed_options or parsed_expose_to:
                await capture_entity_registry(self._client, entity_id, "ha_set_entity")

            # Phase 3: Send entity registry update (covers all fields except expose_to)
            (
                entity_id,
                entity_entry,
                has_registry_updates,
            ) = await self._execute_registry_update(
                entity_id, message, updates_made, new_entity_id
            )

        # Phase 4: Per-domain options
        entity_entry, options_succeeded = await self._apply_options_updates(
            entity_id, parsed_options, entity_entry, has_registry_updates, updates_made
        )

        # Phase 5: Device rename
        device_rename_result: dict[str, Any] | None = None
        if device_name_blank:
            device_name_warning: str | None = _CLEAR_DEVICE_NAME_HINT
        else:
            device_name_warning = None
            device_rename_result, entity_entry = await self._apply_device_rename(
                entity_id, entity_entry, new_device_name
            )
        if device_rename_result and device_rename_result.get("success"):
            updates_made.append(f"device_name -> {new_device_name}")

        # Phase 6: Expose to assistants
        exposure_result, entity_entry = await self._apply_expose_to(
            entity_id,
            parsed_expose_to,
            entity_entry,
            has_registry_updates,
            options_succeeded,
            device_rename_result,
        )
        if parsed_expose_to is not None:
            updates_made.append(f"expose_to={parsed_expose_to}")

        # Phase 7: Build response
        return self._build_entity_response(
            entity_id,
            original_entity_id,
            entity_entry,
            updates_made,
            new_entity_id,
            exposure_result,
            device_rename_result,
            device_name_warning,
        )

    async def _bulk_apply_expose(
        self,
        entity_ids: list[str],
        parsed_expose_to: dict[str, bool],
    ) -> str | None:
        """Expose/hide many entities in one homeassistant/expose_entity call per set.

        The command's schema accepts the full entity_ids list, so bulk exposure
        needs at most two WS calls (one for the assistants being turned on, one
        for those being turned off) regardless of entity count — not one per
        entity. Returns None on success, or the error message on the first
        failing set. A failure is batch-level: it applies to every id in the
        list, so the caller maps the returned message onto all of them.
        """
        expose_true = [a for a, v in parsed_expose_to.items() if v]
        expose_false = [a for a, v in parsed_expose_to.items() if not v]
        applied: list[str] = []
        for assistants, should_expose in [(expose_true, True), (expose_false, False)]:
            if not assistants:
                continue
            logger.info(
                f"{'Exposing' if should_expose else 'Hiding'} {len(entity_ids)} "
                f"entities {'to' if should_expose else 'from'} {assistants}"
            )
            result = await self._client.send_websocket_message(
                {
                    "type": "homeassistant/expose_entity",
                    "assistants": assistants,
                    "entity_ids": entity_ids,
                    "should_expose": should_expose,
                }
            )
            if not result.get("success"):
                error = _extract_ws_error(result)
                if applied:
                    # The earlier assistant group already changed — say so, or
                    # the all-failed report implies no exposure was touched.
                    error = (
                        f"{error} (note: expose changes for "
                        f"{', '.join(applied)} were already applied before "
                        "this failure)"
                    )
                return error
            applied.extend(assistants)
        return None

    async def _bulk_registry_phase(
        self,
        entity_ids: list[str],
        parsed_categories: dict[str, str | None] | None,
        parsed_labels: list[str] | None,
        label_operation: str,
    ) -> tuple[
        dict[str, dict[str, Any] | None],
        dict[str, list[str]],
        list[dict[str, Any]],
        list[str],
    ]:
        """Per-entity label/category updates (no expose).

        HA's entity_registry/update is single-entity, so this stays a
        per-entity fan-out. Returns (entry_by_id, updates_by_id, failed,
        eligible_ids); eligible_ids are the ids whose registry update
        succeeded (all ids when there is no registry work — expose-only bulk).
        """
        entry_by_id: dict[str, dict[str, Any] | None] = {}
        updates_by_id: dict[str, list[str]] = {}
        failed: list[dict[str, Any]] = []

        if parsed_labels is None and parsed_categories is None:
            for eid in entity_ids:
                updates_by_id[eid] = []
            return entry_by_id, updates_by_id, failed, list(entity_ids)

        eligible_ids: list[str] = []
        results = await asyncio.gather(
            *[
                self._update_single_entity(
                    eid,
                    None,  # area_id not supported in bulk
                    None,  # name not supported in bulk
                    None,  # icon not supported in bulk
                    None,  # enabled not supported in bulk
                    None,  # hidden not supported in bulk
                    None,  # aliases not supported in bulk
                    parsed_categories,
                    parsed_labels,
                    label_operation,
                    None,  # expose_to batched separately below
                )
                for eid in entity_ids
            ],
            return_exceptions=True,
        )
        for eid, result in zip(entity_ids, results, strict=True):
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                # Never swallow cancellation/shutdown into per-entity errors.
                raise result
            if isinstance(result, BaseException):
                error_msg = (
                    extract_tool_error_message(result)
                    if isinstance(result, ToolError)
                    else str(result)
                )
                failed.append({"entity_id": eid, "error": error_msg})
            else:
                # _update_single_entity returns success-shape or raises
                # ToolError (caught above as BaseException).
                entry_by_id[eid] = result.get("entity_entry")
                updates_by_id[eid] = list(result.get("updates") or [])
                eligible_ids.append(eid)
        return entry_by_id, updates_by_id, failed, eligible_ids

    async def _bulk_expose_phase(
        self,
        eligible_ids: list[str],
        parsed_expose_to: dict[str, bool],
        entry_by_id: dict[str, dict[str, Any] | None],
        updates_by_id: dict[str, list[str]],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Batch-expose eligible_ids, then refetch their post-exposure state.

        Returns (still_eligible_ids, newly_failed). A batched-expose failure
        is reported against every eligible id (the command acts on the whole
        list atomically) — matching the old per-entity path where each entity
        raised on its own expose failure — and clears eligibility. On success,
        options mutate, so a single get_entries refetch refreshes each
        entity_entry; a refetch failure keeps the pre-exposure snapshot since
        the exposure already committed.
        """
        expose_error = await self._bulk_expose_with_backup(
            eligible_ids, parsed_expose_to
        )
        if expose_error is not None:
            failed = [
                {"entity_id": eid, "error": f"Exposure failed: {expose_error}"}
                for eid in eligible_ids
            ]
            return [], failed

        for eid in eligible_ids:
            updates_by_id[eid].append(f"expose_to={parsed_expose_to}")
        refetched_raw, refetch_errors = await self._get_entries_raw(eligible_ids)
        for eid in eligible_ids:
            if eid in refetched_raw:
                entry_by_id[eid] = _format_entity_entry(refetched_raw[eid])
        # An id whose refetch failed AND that has no earlier registry snapshot
        # (expose-only bulk never populates one) would otherwise be reported
        # as a success row with entity_entry=None — e.g. a typo'd or deleted
        # entity. The old per-entity path raised for exactly this case.
        still_eligible: list[str] = []
        newly_failed: list[dict[str, Any]] = []
        for eid in eligible_ids:
            if eid in refetch_errors and entry_by_id.get(eid) is None:
                newly_failed.append(
                    {
                        "entity_id": eid,
                        "error": (
                            "Exposure command was sent, but the entity could "
                            "not be verified in the registry: "
                            f"{refetch_errors[eid]}"
                        ),
                    }
                )
            else:
                still_eligible.append(eid)
        if refetch_errors:
            logger.warning(
                "Bulk exposure applied but post-exposure refresh failed "
                "for %d id(s); %d had a pre-exposure snapshot to return",
                len(refetch_errors),
                len(refetch_errors) - len(newly_failed),
            )
        return still_eligible, newly_failed

    async def _bulk_expose_with_backup(
        self, eligible_ids: list[str], parsed_expose_to: dict[str, bool]
    ) -> str | None:
        async with AsyncExitStack() as owners:
            for eid in sorted(set(eligible_ids)):
                await owners.enter_async_context(registry_update_lock("entity", eid))
            if parsed_expose_to:
                for eid in dict.fromkeys(eligible_ids):
                    await capture_entity_registry(self._client, eid, "ha_set_entity")
            return await self._bulk_apply_expose(eligible_ids, parsed_expose_to)

    async def _bulk_update_entities(
        self,
        entity_ids: list[str],
        parsed_categories: dict[str, str | None] | None,
        parsed_labels: list[str] | None,
        label_operation: str,
        parsed_expose_to: dict[str, bool] | None,
    ) -> dict[str, Any]:
        """Apply bulk label/category/expose updates across many entities.

        Registry work runs per-entity (_bulk_registry_phase); only the
        expose_to phase is batched into one homeassistant/expose_entity call
        per assistant-set (_bulk_expose_phase).
        """
        logger.info(f"Bulk updating {len(entity_ids)} entities")
        if (
            parsed_labels is None
            and parsed_categories is None
            and parsed_expose_to is None
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "No updates specified",
                    suggestions=[
                        "Provide at least one of: labels, categories, or expose_to"
                    ],
                )
            )

        (
            entry_by_id,
            updates_by_id,
            failed,
            eligible_ids,
        ) = await self._bulk_registry_phase(
            entity_ids, parsed_categories, parsed_labels, label_operation
        )

        if parsed_expose_to is not None and eligible_ids:
            eligible_ids, expose_failed = await self._bulk_expose_phase(
                eligible_ids, parsed_expose_to, entry_by_id, updates_by_id
            )
            failed.extend(expose_failed)

        succeeded_ids = set(eligible_ids)
        succeeded_list = [
            {
                "entity_id": eid,
                "entity_entry": entry_by_id.get(eid),
                "updates": updates_by_id.get(eid, []),
            }
            for eid in entity_ids
            if eid in succeeded_ids
        ]

        response: dict[str, Any] = {
            "success": len(failed) == 0,
            "total": len(entity_ids),
            "succeeded_count": len(succeeded_list),
            "failed_count": len(failed),
            "succeeded": succeeded_list,
        }
        if failed:
            response["failed"] = failed
            response["partial"] = len(succeeded_list) > 0
        return response

    async def _fetch_entity(self, eid: str) -> dict[str, Any]:
        """Fetch a single entity from the registry."""
        message: dict[str, Any] = {
            "type": "config/entity_registry/get",
            "entity_id": eid,
        }
        result = await self._client.send_websocket_message(message)

        if not result.get("success"):
            raise ValueError(_extract_ws_error(result))

        return _format_fetched_entity(result.get("result") or {})

    async def _get_entries_raw(
        self, entity_ids: list[str]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """Bulk-fetch registry entries via chunked config/entity_registry/get_entries.

        Returns ``(raw_entries, errors)``: ``raw_entries`` maps a found
        entity_id to HA's raw extended registry dict; ``errors`` maps a failed
        entity_id to a message. A ``null`` in HA's entries map (id not in the
        registry) and a chunk-level WS failure both land in ``errors`` — this
        reproduces the per-id not-found contract of the old one-get-per-id
        fan-out, where every id got its own error. Callers project the raw
        entries through whichever formatter their response shape needs.
        """
        raw: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        if not entity_ids:
            return raw, errors

        chunks = [
            entity_ids[i : i + _GET_ENTRIES_CHUNK_SIZE]
            for i in range(0, len(entity_ids), _GET_ENTRIES_CHUNK_SIZE)
        ]
        responses = await asyncio.gather(
            *(
                self._client.send_websocket_message(
                    {
                        "type": "config/entity_registry/get_entries",
                        "entity_ids": chunk,
                    }
                )
                for chunk in chunks
            ),
            return_exceptions=True,
        )
        for chunk, resp in zip(chunks, responses, strict=True):
            if isinstance(resp, BaseException) and not isinstance(resp, Exception):
                # Never swallow cancellation/shutdown into per-entity errors.
                raise resp
            if isinstance(resp, dict) and resp.get("success"):
                result_map = resp.get("result") or {}
                for eid in chunk:
                    entry = result_map.get(eid)
                    if entry is None:
                        errors[eid] = "Entity not found"
                    else:
                        raw[eid] = entry
                continue
            # Whole-chunk failure (WS raised, or success=False): every id in
            # the chunk failed, mirroring the old per-id fan-out behaviour.
            msg = _extract_ws_error(resp) if isinstance(resp, dict) else str(resp)
            for eid in chunk:
                errors[eid] = msg
        return raw, errors

    async def set(
        self,
        entity_id: str | list[str],
        area_id: str | None = None,
        name: str | None = None,
        use_device_name: bool | None = None,
        icon: str | None = None,
        device_class: str | None = None,
        options: dict[str, dict[str, Any]] | None = None,
        enabled: bool | None = None,
        hidden: bool | None = None,
        aliases: str | list[str | None] | None = None,
        use_entity_name_alias: bool | None = None,
        categories: dict[str, str | None] | None = None,
        labels: str | list[str] | None = None,
        label_operation: Literal["set", "add", "remove"] = "set",
        expose_to: dict[str, bool] | None = None,
        new_entity_id: str | None = None,
        new_device_name: str | None = None,
    ) -> dict[str, Any]:
        try:
            entity_ids, is_bulk = _parse_set_entity_ids(entity_id)

            # Per-element empty/whitespace check — the list-empty check above
            # rejects ``[]`` but not ``[""]``; without this guard, an empty
            # entity_id would propagate to the entity-registry update WS call
            # and surface as a misleading HA "entity not found".
            for eid in entity_ids:
                validate_identifier_not_empty(eid, "entity_id")

            # Validate: bulk operations only support categories, labels, and expose_to
            single_entity_params = {
                "area_id": area_id,
                "name": name,
                "use_device_name": use_device_name,
                "icon": icon,
                "device_class": device_class,
                "options": options,
                "enabled": enabled,
                "hidden": hidden,
                "aliases": aliases,
                "use_entity_name_alias": use_entity_name_alias,
                "new_entity_id": new_entity_id,
                "new_device_name": new_device_name,
            }
            non_null_single_params = [
                k for k, v in single_entity_params.items() if v is not None
            ]
            if is_bulk and non_null_single_params:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Bulk operations (multiple entity_ids) only support categories, labels, and expose_to. "
                        f"Single-entity parameters provided: {non_null_single_params}",
                        suggestions=[
                            "Use a single entity_id for area_id, name, use_device_name, icon, device_class, options, enabled, hidden, aliases, or use_entity_name_alias",
                            "Or remove single-entity parameters to use bulk categories/labels/expose_to",
                        ],
                    )
                )

            _validate_enabled_constraint(enabled, entity_ids)

            parsed_aliases = _parse_aliases_param(aliases)
            parsed_categories = _parse_categories_param(categories)
            parsed_labels = _parse_string_list_field(labels, "labels")
            parsed_options = _parse_options_param(options)
            parsed_expose_to = _parse_expose_to_param(expose_to)

            # Reject invalid shared references before any bulk writes. Each
            # entity also revalidates under its lock to catch references deleted
            # while waiting, preserving per-entity partial-progress reporting.
            if is_bulk:
                await validate_registry_ids(
                    self._client,
                    None,
                    parsed_labels if label_operation in ("set", "add") else None,
                    parsed_categories,
                    fail_closed=True,
                )

            # Single entity case
            if not is_bulk:
                return await self._update_single_entity(
                    entity_ids[0],
                    area_id,
                    name,
                    icon,
                    enabled,
                    hidden,
                    parsed_aliases,
                    parsed_categories,
                    parsed_labels,
                    label_operation,
                    parsed_expose_to,
                    new_entity_id=new_entity_id,
                    new_device_name=new_device_name,
                    device_class=device_class,
                    parsed_options=parsed_options,
                    use_entity_name_alias=use_entity_name_alias,
                    use_device_name=use_device_name,
                )

            # Bulk case
            return await self._bulk_update_entities(
                entity_ids,
                parsed_categories,
                parsed_labels,
                label_operation,
                parsed_expose_to,
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error updating entity: {e}")
            exception_to_structured_error(e, context={"entity_id": entity_id})
            return None  # unreachable: exception_to_structured_error always raises

    ha_get_entity = tool(
        name="ha_get_entity",
        tags={"Entity Registry"},
        annotations=read_only_hints("Get Entity", open_world=False),
    )(log_tool_usage(legacy_errors(EntityDiscoveryTools.ha_get_entity)))

    _raise_entity_error = staticmethod(raise_entity_read_error)

    async def _fetch_enrichment(
        self, entity_ids: list[str]
    ) -> dict[str, dict[str, Any]] | None:
        return await fetch_entity_enrichment_via_component(self._client, entity_ids)

    async def _list_entity_registry(self, unique_id: str) -> list[dict[str, Any]]:
        list_resp = await self._client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )
        if not list_resp.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list entity registry: {_extract_ws_error(list_resp)}",
                    context={"unique_id": unique_id},
                    suggestions=["Check Home Assistant connection and retry"],
                )
            )
        rows: list[dict[str, Any]] = list_resp.get("result") or []
        return rows

    async def _bulk_remove_entities(self, entity_ids: list[str]) -> dict[str, Any]:
        """Remove many entities sequentially, classifying each outcome.

        Registry writes are NOT parallelized (per the removal WS command's
        write semantics). Each id lands in exactly one bucket: ``removed``,
        ``skipped`` (not-found is idempotent success, not an error), or
        ``errors`` (any other failure, carrying code + message).
        """
        if not isinstance(entity_ids, list):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"entity_id must be a string or list of strings, got "
                    f"{type(entity_ids).__name__}",
                )
            )
        if not entity_ids:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "entity_id list cannot be empty",
                )
            )
        if not all(isinstance(e, str) for e in entity_ids):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "All entity_id values must be strings",
                )
            )
        if len(entity_ids) > _MAX_BULK_REMOVE:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Too many entity IDs: {len(entity_ids)} exceeds maximum "
                    f"of {_MAX_BULK_REMOVE}",
                )
            )
        for eid in entity_ids:
            validate_identifier_not_empty(
                eid,
                "entity_id",
                suggestions=["Use ha_search() to find valid entity IDs"],
            )

        logger.info(f"Bulk removing {len(entity_ids)} entities")
        removed: list[str] = []
        skipped: list[str] = []
        errors: list[dict[str, str]] = []
        for eid in entity_ids:
            async with registry_update_lock("entity", eid):
                await capture_entity_registry(self._client, eid, "ha_remove_entity")
                result = await self._client.send_websocket_message(
                    {"type": "config/entity_registry/remove", "entity_id": eid}
                )
            if result.get("success"):
                removed.append(eid)
                continue
            error_msg = _extract_ws_error(result)
            # Same not-found detection as the single path — HA surfaces the
            # error as a plain string; already-absent is idempotent success.
            if "not found" in error_msg.lower():
                skipped.append(eid)
            else:
                errors.append(
                    {
                        "entity_id": eid,
                        "code": ErrorCode.SERVICE_CALL_FAILED.value,
                        "message": error_msg,
                    }
                )

        return {
            "success": len(errors) == 0,
            "total": len(entity_ids),
            "removed": removed,
            "skipped": skipped,
            "errors": errors,
        }

    async def remove(self, entity_id: str | list[str]) -> dict[str, Any]:
        try:
            # List mode: bulk removal with a per-id classification envelope.
            if not isinstance(entity_id, str):
                return await self._bulk_remove_entities(entity_id)

            # Empty/whitespace entity_id would reach the registry-remove WS
            # command and surface as a misleading HA "entity not found".
            validate_identifier_not_empty(
                entity_id,
                "entity_id",
                suggestions=[
                    "Use ha_search() to find valid entity IDs",
                ],
            )
            async with registry_update_lock("entity", entity_id):
                await capture_entity_registry(
                    self._client, entity_id, "ha_remove_entity"
                )
                result, notes = await remove_registry_entry(self._client, entity_id)

            if not result.get("success"):
                error_msg = _extract_ws_error(result)
                if "not found" in error_msg.lower():
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.ENTITY_NOT_FOUND,
                            f"Entity '{entity_id}' not found in registry",
                            context={"entity_id": entity_id},
                            suggestions=[
                                "Use ha_search() to find valid entity IDs",
                                "The entity may have already been removed",
                            ],
                        )
                    )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to remove entity '{entity_id}': {error_msg}",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Check HA logs for details on why the removal was rejected",
                        ],
                    )
                )

            return {"success": True, "entity_id": entity_id} | (
                {"warnings": notes} if notes else {}
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error removing entity '{entity_id}': {e}")
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id},
            )
            return None  # unreachable: exception_to_structured_error always raises


def register_entity_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register entity management tools with the MCP server."""
    register_tool_methods(mcp, EntityTools(client))


__all__ = [
    "EntityTools",
    "fetch_entity_enrichment_via_component",
    "register_entity_tools",
]
