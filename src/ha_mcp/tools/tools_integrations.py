"""
Integration management tools for Home Assistant MCP server.

This module provides tools to list, enable, disable, and delete Home Assistant
integrations (config entries) via the REST and WebSocket APIs.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Literal, NoReturn, cast

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import LegacyBackendTools, entry_write_guard, legacy_errors
from ..backends.legacy_flows import legacy_flow, legacy_helper_flow
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..runtime import integration_inspection as _shared_inspection
from ..runtime.helper_removal_storage import (
    _raise_registry_read_failure as _shared_registry_read_failure,
)
from ..runtime.integration_reads import IntegrationReadTools
from ..runtime.integration_removals import IntegrationRemovalTools
from ..runtime.integration_writes import IntegrationWriteTools
from ..tool_api import ToolFailure
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .component_registry_lookup import resolve_entities_via_component
from .config_helpers.schemas import StorageHelperType
from .flow_helper_lookup import (
    resolve_helper_entity,
)
from .helper_flows import require_helper_type
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import read_only_hints, write_hints
from .ws_waiters import wait_for_entity_removed

logger = logging.getLogger(__name__)

# First wait between helper registry lookups; each later retry doubles it.
_REGISTRY_RETRY_BASE_DELAY = 0.5


_raise_registry_read_failure = cast(
    Callable[..., NoReturn], legacy_flow(_shared_registry_read_failure)
)


# The ``ha_mcp_tools`` component command that serves config entries (identity +
# already-materialized ``options`` + ``subentries``) from HA's live registry in
# one in-process frame, replacing the REST list-all + OptionsFlow start/abort
# dance + subentries WS call. Module-local constant per the component-routing
# idiom (see ``component_devices.WS_DEVICE_GET``).
WS_CONFIG_ENTRIES = "ha_mcp_tools/config_entries"


# ha_remove_helpers_integrations.helper_type: a storage helper type,
# "config_subentry", or one of Core's helper flows, checked at call time.
HelperTypeLiteral = StorageHelperType | Literal["config_subentry"] | str


options_from_form_flow = legacy_helper_flow(_shared_inspection.options_from_form_flow)


fetch_entry_options_with_status = legacy_helper_flow(
    _shared_inspection.fetch_entry_options_with_status
)


async def _fetch_entries_via_component(
    client: Any, *, entry_id: str | None = None, domain: str | None = None
) -> list[dict[str, Any]] | None:
    """One ``ha_mcp_tools/config_entries`` read; ``None`` ⇒ run the legacy path.

    Returns the component's ``entries`` list — each row in the
    ``config_entries/get`` shape (identity + status fields plus the entry's
    already-materialized ``options`` [raw persisted, secret-scrubbed] and its
    ``subentries`` identity rows) — so a single in-process frame replaces the
    legacy REST list-all + OptionsFlow start/abort probe + subentries WS call.
    Pass ``entry_id`` for the single entry (empty list ⇒ no such entry) or
    ``domain`` to filter the list; neither lists all.

    Consumers: ``ha_get_integration`` (single-entry + list) and radio's
    ``resolve_entry_id`` (single-instance domain → entry_id) both import this so
    the domain/entry-scoped read routes through one place.

    ``None`` on capability miss, downgrade (``unknown_command`` → invalidate the
    cached caps), or command error/timeout (logged) — the caller falls back to
    its legacy path.

    Per the uniform transport-fallback taxonomy, a connection-establishment
    failure IS caught here and mapped to ``None`` (legacy fallback), like every
    component fetch helper. The callers' legacy paths are NOT the shared pooled WS —
    ``ha_get_integration`` reads pure REST (``get_config_entry`` /
    ``GET /config/config_entries`` + the REST OptionsFlow probe) and radio's
    ``resolve_entry_id`` uses the REST client's ``send_websocket_message``
    bridge — so a WS outage must not kill the tool when the legacy path can
    still serve the entry. (The bridge itself raises on a dead transport since
    #1947, which only matters for the radio caller: when the socket is dead
    that path has nothing to serve either, while the pure-REST caller is
    unaffected.) The catch stays broad so no unexpected fault escapes and
    kills the tool; routing any
    non-command component failure back to the legacy fetch is safe here (mirrors
    ``get_component_caps``' own broad-catch precedent). Otherwise the same caps-gate
    discipline as ``component_devices.fetch_device_via_component``.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "config_entries"):
        return None
    kwargs: dict[str, Any] = {}
    if entry_id is not None:
        kwargs["entry_id"] = entry_id
    if domain is not None:
        kwargs["domain"] = domain
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        raw = await ws.send_command(WS_CONFIG_ENTRIES, **kwargs)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_CONFIG_ENTRIES, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        # DEVIATION (see docstring): the legacy path is pure REST / the REST-client
        # WS bridge, NOT the shared pooled WS. A pooled-WS drop
        # (HomeAssistantConnectionError) OR get_websocket_client() raising a plain
        # Exception when WebSocketManager can't (re)connect must fall back to legacy
        # rather than kill the tool.
        logger.warning(
            "%s connection error; falling back to legacy: %r",
            WS_CONFIG_ENTRIES,
            exc,
        )
        return None
    result = raw.get("result")
    entries = result.get("entries") if isinstance(result, dict) else None
    if not isinstance(entries, list):
        logger.debug(
            "%s returned a malformed result (no 'entries' list); falling back to legacy",
            WS_CONFIG_ENTRIES,
        )
        return None
    return entries


class IntegrationTools(
    LegacyBackendTools,
    IntegrationRemovalTools,
    IntegrationWriteTools,
    IntegrationReadTools,
):
    """Integration management tools for Home Assistant."""

    ha_get_integration = tool(
        name="ha_get_integration",
        tags={"Integrations"},
        annotations=read_only_hints("Get Integration", open_world=False),
    )(
        log_tool_usage(
            legacy_errors(legacy_flow(IntegrationReadTools.ha_get_integration))
        )
    )

    _get_entry_detail_response = legacy_errors(
        legacy_flow(IntegrationReadTools._get_entry_detail_response)
    )

    _ignored_diagnostics_detail_params = staticmethod(
        legacy_flow(IntegrationReadTools._ignored_diagnostics_detail_params)
    )

    _ignored_subentry_detail_params = staticmethod(
        legacy_flow(IntegrationReadTools._ignored_subentry_detail_params)
    )

    _get_single_entry = legacy_errors(
        legacy_flow(IntegrationReadTools._get_single_entry)
    )

    _probe_legacy_entry_options = legacy_errors(
        legacy_flow(IntegrationReadTools._probe_legacy_entry_options)
    )

    _single_entry_from_component = legacy_errors(
        legacy_flow(IntegrationReadTools._single_entry_from_component), classify=False
    )

    _attach_knx_project = legacy_errors(
        legacy_flow(IntegrationReadTools._attach_knx_project)
    )

    _fetch_config_subentries = legacy_errors(
        legacy_flow(IntegrationReadTools._fetch_config_subentries)
    )

    _fetch_config_subentry_schema = legacy_errors(
        legacy_flow(IntegrationReadTools._fetch_config_subentry_schema)
    )

    _options_from_form_flow = staticmethod(
        legacy_flow(IntegrationReadTools._options_from_form_flow)
    )

    _redact_component_options = legacy_errors(
        legacy_flow(IntegrationReadTools._redact_component_options)
    )

    _fetch_options_schema = legacy_errors(
        legacy_flow(IntegrationReadTools._fetch_options_schema)
    )

    _list_entries = legacy_errors(legacy_flow(IntegrationReadTools._list_entries))

    _list_entries_from_component = legacy_errors(
        legacy_flow(IntegrationReadTools._list_entries_from_component)
    )

    _finalize_entry_list = legacy_flow(IntegrationReadTools._finalize_entry_list)

    _format_entry = staticmethod(legacy_flow(IntegrationReadTools._format_entry))

    _filter_by_query = staticmethod(legacy_flow(IntegrationReadTools._filter_by_query))

    ha_set_integration = tool(
        name="ha_set_integration",
        tags={"Integrations"},
        annotations=write_hints(
            "Set Integration", destructive=True, idempotent=False, open_world=False
        ),
    )(
        log_tool_usage(
            legacy_errors(legacy_flow(IntegrationWriteTools.ha_set_integration))
        )
    )

    ha_remove_helpers_integrations = tool(
        name="ha_remove_helpers_integrations",
        tags={"Helper Entities", "Integrations"},
        annotations=write_hints(
            "Remove Helper or Integration",
            destructive=True,
            idempotent=True,
            open_world=False,
        ),
    )(
        log_tool_usage(
            legacy_errors(IntegrationRemovalTools.ha_remove_helpers_integrations)
        )
    )

    # Private helpers keep the ``_delete_*`` prefix because they wrap HA's
    # own backend verb — the WebSocket API is ``<type>/delete`` and the
    # REST API is HTTP DELETE. The public tool surface uses ``remove`` to
    # join the ``ha_remove_*`` behavioural family; the prefix asymmetry is
    # intentional and prevents future renames pulled by either side.

    _remove_resolved_helper = legacy_errors(
        IntegrationRemovalTools._remove_resolved_helper
    )

    async def _resolve_helper_entity(
        self, entity_id: str
    ) -> tuple[HelperTypeLiteral, str]:
        """Map an entity_id to the (helper_type, target) that removes its helper."""
        try:
            return cast(
                tuple[HelperTypeLiteral, str],
                await resolve_helper_entity(self._client, entity_id),
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            # Keep the classified suggestions (auth, connection) and add the
            # entry_id route, which does not resolve the entity in the registry.
            error = exception_to_structured_error(
                e, context={"target": entity_id}, raise_error=False
            )
            error["error"].setdefault("suggestions", []).append(
                "Or pass the helper's config entry_id as target "
                "(ha_get_integration() lists config entries)."
            )
            raise_tool_error(error)
            return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    def _classify_removal_error(
        self,
        error: Exception,
        context: dict[str, Any] | None = None,
        *,
        suggestions: list[str] | None = None,
    ) -> NoReturn:
        if isinstance(error, (ToolError, ToolFailure)):
            raise error
        exception_to_structured_error(error, context=context, suggestions=suggestions)

    _raise_inspection_error = _classify_removal_error

    def _entry_guard(self, entry_id: str) -> Any:
        return entry_write_guard(self._client, entry_id)

    _entry_write_guard = _entry_guard

    async def _wait_removed(self, entity_id: str) -> bool:
        return await wait_for_entity_removed(self._client, entity_id)

    async def _verify_removed(self, entity_id: str, response: dict[str, Any]) -> None:
        from .ws_waiters import verify_entity_removed

        await verify_entity_removed(self._client, entity_id, response)

    # === Path 3: Direct config entry delete (any integration) ===
    _delete_direct_entry = legacy_errors(IntegrationRemovalTools._delete_direct_entry)

    async def _require_remove_type(self, helper_type: str) -> None:
        await require_helper_type(self._client, helper_type)

    async def _read_remove_entry(self, entry_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._client.get_config_entry(entry_id)
        return result

    # === Path 2: FLOW helper delete via entity_id → entry_id lookup ===
    _delete_flow_helper = legacy_errors(IntegrationRemovalTools._delete_flow_helper)

    _delete_resolved_flow_helper = legacy_errors(
        IntegrationRemovalTools._delete_resolved_flow_helper
    )

    _delete_flow_config_entry = legacy_errors(
        IntegrationRemovalTools._delete_flow_config_entry
    )

    _delete_config_subentry = legacy_errors(
        IntegrationRemovalTools._delete_config_subentry
    )

    # === Path 1: SIMPLE helper delete via websocket ===
    _delete_simple_helper = legacy_errors(IntegrationRemovalTools._delete_simple_helper)

    async def _resolve_helper_unique_id(
        self, entity_id: str
    ) -> tuple[str | None, dict[str, Any] | None, bool]:
        """Resolve a SIMPLE helper's unique_id from the entity registry.

        Returns ``(unique_id, registry_result, component_used)``. When the
        component advertises registry_lookup, ONE in-process read replaces the
        3-attempt exponential-backoff loop. That loop absorbed the
        registry-registration LAG between a helper's creation and its entity
        landing in the registry index (not a WS-timing race — the legacy read
        hits the same live registry over the same socket); the single read is
        equally subject to that lag, but a bare id is resolved by its storage
        id before this read, and a stale/missing resolve is classified, never
        turned into a delete by a guessed id.
        On capability miss / component error the legacy retry loop runs
        unchanged. ``component_used`` records which path served the read so the
        exhausted-fallback detail can word the "3 attempts" text accurately.
        """
        client = self._client
        component = await resolve_entities_via_component(client, [entity_id])
        if component is not None:
            found = component.get("entities") or []
            if found:
                # Shape a config/entity_registry/get-style ack so the
                # exhausted-fallback err_detail below reads registry_result
                # uniformly across both the component and legacy paths.
                registry_result: dict[str, Any] = {"success": True, "result": found[0]}
                unique_id = found[0].get("unique_id")
                if unique_id:
                    logger.info(f"Found unique_id: {unique_id} for {entity_id}")
                return unique_id, registry_result, True
            # Assignment form (not a dict literal in the return) keeps the
            # no-return-success-false AST rule scoped to real tool returns:
            # this is an internal registry-ack shape, not an MCP tool response.
            miss_result: dict[str, Any] = {
                "success": False,
                "error": "not found in entity registry",
            }
            return None, miss_result, True
        unique_id, legacy_result = await self._resolve_unique_id_via_registry_retry(
            entity_id
        )
        return unique_id, legacy_result, False

    async def _resolve_unique_id_via_registry_retry(
        self, entity_id: str
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Legacy 3-retry registry lookup for a helper's unique_id.

        Returns ``(unique_id, registry_result)`` — ``registry_result`` is the
        last WebSocket response (or None if none arrived), used by the caller's
        exhausted-fallback detail.
        """
        client = self._client
        unique_id = None
        registry_result: dict[str, Any] | None = None
        max_retries = 3
        for attempt in range(max_retries):
            logger.info(
                f"Getting entity registry for: {entity_id} "
                f"(attempt {attempt + 1}/{max_retries})"
            )

            # State check is informational only — disabled entities are
            # missing from the state machine but resolved via the registry
            # below (issue #1057). Kept as a debug breadcrumb rather than
            # removed; full removal is option 3.2 in #1057, deferred to a
            # separate PR for minimal blast radius here.
            try:
                state_check = await client.get_entity_state(entity_id)
                if not state_check:
                    logger.debug(
                        f"Entity {entity_id} not in state; "
                        "proceeding to registry lookup"
                    )
            except HomeAssistantAPIError as e:
                # State check is best-effort here; an APIError (e.g. 404)
                # is informational. Auth/connection errors must propagate
                # so they're not re-reported as ENTITY_NOT_FOUND below.
                logger.debug(f"State check failed for {entity_id}: {e}")

            # Registry lookup
            registry_msg: dict[str, Any] = {
                "type": "config/entity_registry/get",
                "entity_id": entity_id,
            }
            try:
                registry_result = await client.send_websocket_message(registry_msg)
                if (registry_result or {}).get("success"):
                    entity_entry = (registry_result or {}).get("result") or {}
                    unique_id = entity_entry.get("unique_id")
                    if unique_id:
                        logger.info(f"Found unique_id: {unique_id} for {entity_id}")
                        break
                if attempt < max_retries - 1:
                    wait_time = _REGISTRY_RETRY_BASE_DELAY * (2**attempt)
                    logger.debug(
                        f"Registry lookup failed for {entity_id}, "
                        f"waiting {wait_time}s before retry..."
                    )
                    await asyncio.sleep(wait_time)
            except HomeAssistantAPIError as e:
                # APIError (e.g. 404) is informational and worth a retry.
                # Auth/connection errors must propagate so they're not
                # re-reported as ENTITY_NOT_FOUND in the fallback below.
                logger.warning(f"Registry lookup attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    wait_time = _REGISTRY_RETRY_BASE_DELAY * (2**attempt)
                    await asyncio.sleep(wait_time)
        return unique_id, registry_result

    _delete_simple_helper_fallback = legacy_errors(
        IntegrationRemovalTools._delete_simple_helper_fallback
    )

    async def _state_absent(self, entity_id: str) -> bool:
        """Return True when ``entity_id`` is absent from the state machine.

        A non-404 APIError (transient/auth) is re-raised so it is not
        mis-classified as a missing target; a 404 is treated as state-absent.
        """
        client = self._client
        try:
            final_state_check = await client.get_entity_state(entity_id)
            return not final_state_check
        except HomeAssistantAPIError as e:
            # Only 404 confirms the entity is absent from the state
            # machine. Other API failures (500, 401, …) are transient
            # or auth issues and must propagate so they aren't
            # mis-classified as a missing target. Mirrors the
            # status_code == 404 narrow in _delete_direct_entry.
            if e.status_code != 404:
                raise
            logger.debug(
                f"State check for {entity_id} raised 404 "
                f"(treating as state-absent): {e}"
            )
            return True

    async def _registry_still_has_entry(self, target: str, entity_id: str) -> bool:
        """Return True if ``entity_id`` still has an entity-registry entry.

        Only Core's ``not_found``, or its ``invalid_format`` for an id that
        cannot name an entity, confirms absence. Any other failed read, and a
        reply without an entry, is raised as the read failure, so a disabled
        helper behind a blocked read is not reported as already removed.
        """
        reply = await self._client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
        if (reply or {}).get("success"):
            if (reply.get("result") or {}).get("entity_id"):
                return True
            reply = {"error": "the reply carried no registry entry"}
        elif (reply or {}).get("error_code") in ("not_found", "invalid_format"):
            return False
        _raise_registry_read_failure(target, entity_id, reply)
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    _delete_simple_via_unique_id = legacy_errors(
        IntegrationRemovalTools._delete_simple_via_unique_id
    )


def register_integration_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register integration management tools with the MCP server."""
    register_tool_methods(mcp, IntegrationTools(client))


__all__ = [
    "IntegrationTools",
    "fetch_entry_options_with_status",
    "options_from_form_flow",
    "register_integration_tools",
]
