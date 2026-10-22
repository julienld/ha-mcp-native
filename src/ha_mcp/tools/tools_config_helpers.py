"""
Configuration management tools for Home Assistant helpers.

This module provides tools for listing, creating, updating, and removing
Home Assistant helper entities (input_button, input_boolean, input_select,
input_number, input_text, input_datetime, counter, timer, schedule).
"""

import logging
from typing import Any, cast

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import LegacyBackendTools, legacy_errors
from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from ..runtime.helper_listing import HelperListingTools
from ..runtime.helper_tools import HelperConfigTools as SharedHelperConfigTools
from ..tool_api import ToolFailure
from .auto_backup import with_auto_backup
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .config_entry_backup import helper_backup_id
from .config_helpers.describe import describe_helper_response
from .config_helpers.flow import _handle_flow_helper, _handle_set_config_subentry
from .config_helpers.listing import (
    _component_covers,
    _raise_all_requires_component,
    _raise_flow_requires_component,
    _shape_component_helpers_response,
    raise_if_helper_flows_degraded,
    shape_all_helpers_response,
)
from .config_helpers.registry import (
    _check_name_collision,
    _enrich_helpers_with_current_registry,
    _flatten_helper_list_result,
    validate_registry_ids,
)
from .config_helpers.schemas import (
    SIMPLE_HELPER_TYPES,
    _attach_helper_skill,
)
from .config_helpers.typed_config import _core_schema_context, _prepare_typed_params
from .config_helpers.validation import _validate_set_helper_action
from .config_write_helpers import (
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
)
from .helper_flows import helper_flow_types, require_helper_type
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)


class HelperConfigTools(
    LegacyBackendTools, SharedHelperConfigTools, HelperListingTools
):
    """Encapsulates helper configuration tools for ha_config_list_helpers and ha_config_set_helper."""

    ha_config_list_helpers = tool(
        name="ha_config_list_helpers",
        tags={"Helper Entities"},
        annotations=read_only_hints("List Helpers", open_world=False),
    )(log_tool_usage(legacy_errors(HelperListingTools.ha_config_list_helpers)))

    async def _require_list_helper_type(self, helper_type: str) -> None:
        await require_helper_type(self._client, helper_type, "all")

    async def _describe_helper(
        self,
        helper_type: str,
        menu_choice: str | None,
        helper_id: str | None,
        *,
        describe: bool,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await describe_helper_response(
                self._client, helper_type, menu_choice, helper_id, describe=describe
            ),
        )

    async def _list_helper_records(self, helper_type: str) -> dict[str, Any]:
        # All-types mode: one merged component listing across every helper type.
        # No legacy equivalent exists (no single WS command enumerates all
        # types), so it is component-only — see ``_list_all_helpers``.
        if helper_type == "all":
            return await self._list_all_helpers()

        # Flow-based helper types have no ``{type}/list`` command, so only the
        # component's ``helpers_list`` can enumerate them: they are served
        # exclusively through the component path (never the legacy body, never a
        # silent empty). Storage/collection types keep the legacy fallback.
        is_flow = helper_type not in SIMPLE_HELPER_TYPES

        # Prefer the custom component's in-process listing when it advertises
        # the capability: one WS round-trip that joins the entity registry, so
        # each record carries the current entity_id + display name (the #1794
        # stale-id fix, shipped additively) instead of only the storage id.
        # Fall back cleanly for storage types when the component is absent,
        # downlevel, or errors — the taxonomy lives in
        # ``_list_helpers_via_component``. The legacy body below is untouched.
        caps = await get_component_caps(self._client)
        if component_supports(caps, "helpers_list"):
            component_response = await self._list_helpers_via_component(
                helper_type, is_flow=is_flow
            )
            if component_response is not None:
                return component_response
        if is_flow:
            # No usable component path and the legacy body cannot serve flow
            # helpers: hard error rather than an empty or misleading list.
            _raise_flow_requires_component(helper_type)
        try:
            result = await self._client.send_websocket_message(
                {"type": f"{helper_type}/list"}
            )
            if result.get("success"):
                # Flatten first: person/list returns {"storage": [...],
                # "config": [...]} rather than a flat list, so a raw
                # result["result"] would be a dict here — breaking both the
                # count and the registry enrichment below (which iterates
                # records). _flatten_helper_list_result normalises both shapes.
                items = _flatten_helper_list_result(result)
                warnings = await _enrich_helpers_with_current_registry(
                    self._client, helper_type, items
                )
                response: dict[str, Any] = {
                    "success": True,
                    "helper_type": helper_type,
                    "count": len(items),
                    "helpers": items,
                    "message": f"Found {len(items)} {helper_type} helper(s)",
                }
                if warnings:
                    response["warnings"] = warnings
                return response
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list helpers: {result.get('error', 'Unknown error')}",
                    context={"helper_type": helper_type},
                )
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error listing helpers: {e}")
            exception_to_structured_error(
                e,
                context={"helper_type": helper_type},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                    "Use ha_search(domain_filter='input_*') as alternative",
                ],
            )
            return (
                None  # exception_to_structured_error always raises; explicit for CodeQL
            )
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    async def _list_helpers_via_component(
        self, helper_type: str, *, is_flow: bool
    ) -> dict[str, Any] | None:
        """Serve ha_config_list_helpers from the component; ``None`` ⇒ legacy.

        Error taxonomy (component_api design § 4), with a flow-helper override
        (a flow type has no legacy path, so any component failure is fatal):

        - ``unknown_command`` (the cached positive caps went stale after a
          component downgrade): invalidate the caps. For a storage type, return
          ``None`` so the caller falls through to the byte-identical legacy
          body, **silently**. For a flow type, raise the component-required
          error.
        - any other ``HomeAssistantCommandError`` (a component handler bug) or a
          ``HomeAssistantCommandTimeout`` (the component WS list timed out):
          for a storage type, serve the correct result from the legacy WS list,
          append a ``warnings[]`` entry, and ``log.warning``. For a flow type,
          raise the component-required error — no legacy fallback exists.
        - ``HomeAssistantConnectionError`` (pooled-WS drop) or the plain
          ``Exception`` ``get_websocket_client()`` raises on a failed (re)connect:
          for a storage type, the legacy ``{helper_type}/list`` body is attempted,
          with a ``warnings[]`` entry + ``log.warning``. It rides the
          ``send_websocket_message`` bridge over the SAME pooled connection
          (``WebSocketManager`` keys one client per url/token/verify_ssl), so it
          recovers a component-side fault but not a dead socket — there the bridge
          raises in turn (#1947) and the failure surfaces instead of being served
          as a short list. For a flow type — which has no legacy body — the
          transport failure re-raises to the tool's structured-error handler.

        On a successful response the type must also be in the component's
        ``covered_types`` (see :func:`_component_covers`): a type the response
        could not authoritatively enumerate (``tag``, or any type on an older
        component with no ``covered_types``) is handled like a component miss —
        storage falls back to legacy silently, flow raises.
        """
        try:
            raw = await self._send_component_helpers_list(helper_type, is_flow=is_flow)
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            unknown = is_unknown_command(exc)
            if unknown:
                invalidate_caps(self._client)
            if is_flow:
                logger.warning(
                    "ha_mcp_tools/helpers_list failed for flow type %r: %r",
                    helper_type,
                    exc,
                )
                _raise_flow_requires_component(helper_type)
            if unknown:
                return None
            legacy = await self._legacy_helper_list(helper_type)
            legacy.setdefault("warnings", []).append(
                f"component helpers_list path failed ({exc}); served via legacy path"
            )
            logger.warning(
                "ha_mcp_tools/helpers_list failed; fell back to legacy: %r", exc
            )
            return legacy
        except Exception as exc:
            # Transport/establishment failure — a pooled-WS drop or a failed
            # (re)connect, both HomeAssistantConnectionError. A storage type
            # still attempts the legacy
            # `{helper_type}/list` body: it rides the same pooled connection, so it
            # recovers a component-side fault but not a dead socket, where the
            # bridge raises in turn (#1947). A flow type has NO legacy body, so its
            # transport failure re-raises to the tool's structured-error handler.
            if is_flow:
                raise
            legacy = await self._legacy_helper_list(helper_type)
            legacy.setdefault("warnings", []).append(
                f"component helpers_list connection error ({exc}); "
                "served via legacy path"
            )
            logger.warning(
                "ha_mcp_tools/helpers_list connection error; fell back to legacy: %r",
                exc,
            )
            return legacy
        result = raw.get("result") or {}
        if not _component_covers(result, helper_type):
            # The component did not authoritatively enumerate this type (tag has
            # no state entity for the from-states scan; or an older component
            # sent no covered_types). Don't trust a partial/empty list: storage
            # types fall back to the legacy path silently; a flow type raises,
            # naming the failed loader read when the component reports one, else
            # the component-required error, rather than emptying out.
            if is_flow:
                raise_if_helper_flows_degraded(result, [helper_type])
                _raise_flow_requires_component(helper_type)
            return None
        return _shape_component_helpers_response(helper_type, result)

    async def _send_component_helpers_list(
        self, helper_type: str, *, is_flow: bool
    ) -> dict[str, Any]:
        """Send one ``ha_mcp_tools/helpers_list`` command over the per-client WS.

        Requests only the single ``helper_type`` and sets
        ``include_flow_helpers`` to match its universe — ``True`` for a
        flow-based type (so the component returns its config-entry-backed
        record), ``False`` for a storage/collection type.
        """
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        return await ws.send_command(
            "ha_mcp_tools/helpers_list",
            helper_types=[helper_type],
            include_flow_helpers=is_flow,
        )

    async def _legacy_helper_list(self, helper_type: str) -> dict[str, Any]:
        """Legacy ``{helper_type}/list`` success envelope, for the § 4 #3 fallback.

        A copy of the tool's inline legacy success path, kept separate rather
        than extracted from that body. It flattens and joins the entity registry
        (issue #1945) exactly like the inline path, so a renamed helper served on
        this fallback carries its current entity_id/name; the caller additionally
        appends a ``warnings[]`` entry flagging that the component path was used.
        """
        result = await self._client.send_websocket_message(
            {"type": f"{helper_type}/list"}
        )
        if not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list helpers: {result.get('error', 'Unknown error')}",
                    context={"helper_type": helper_type},
                )
            )
        # Flatten like the inline body: person/list returns {"storage": [...],
        # "config": [...]} rather than a flat list, so a raw result["result"]
        # would be a dict here — breaking count, the pagination slice and the
        # all-types merge, which all expect a list of records.
        items = _flatten_helper_list_result(result)
        # Join the entity registry like the inline body (issue #1945): without
        # this a renamed helper served on the component-error fallback keeps its
        # stale storage id/name, the same #1794 staleness the inline path fixes.
        enrich_warnings = await _enrich_helpers_with_current_registry(
            self._client, helper_type, items
        )
        response: dict[str, Any] = {
            "success": True,
            "helper_type": helper_type,
            "count": len(items),
            "helpers": items,
            "message": f"Found {len(items)} {helper_type} helper(s)",
        }
        if enrich_warnings:
            response["warnings"] = enrich_warnings
        return response

    async def _list_all_helpers(self) -> dict[str, Any]:
        """Serve ``helper_type="all"``: one merged component listing, or a hard error.

        All-types mode has no legacy equivalent, so it is component-only
        (mirroring the flow-helper precedent). When the component advertises
        ``helpers_list`` it serves the merged listing; otherwise — or if the
        component call fails — this raises COMPONENT_NOT_INSTALLED via
        :func:`_raise_all_requires_component` rather than returning an empty list.
        A raw transport failure (e.g. the WS cannot connect right after an HA
        restart) becomes the structured error the rest of the tool uses — never
        an unclassified exception.
        """
        response: dict[str, Any] | None = None
        try:
            caps = await get_component_caps(self._client)
            if component_supports(caps, "helpers_list"):
                response = await self._all_helpers_via_component()
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"helper_type": "all"},
                suggestions=[
                    "Home Assistant may be restarting or unreachable — retry shortly",
                ],
            )
        if response is None:
            _raise_all_requires_component()
            raise AssertionError("Component-required projection must raise")
        return response

    async def _all_helpers_via_component(self) -> dict[str, Any] | None:
        """Serve all-types from the component; ``None`` ⇒ raise component-required.

        There is no legacy all-types path, so — unlike single-type storage
        listing — a failed component command resolves to the same hard error the
        caller raises (mirroring the flow-helper taxonomy); a listing that misses
        a helper flow Core lists raises in ``shape_all_helpers_response``.
        ``unknown_command`` additionally invalidates the now-stale positive caps.
        """
        try:
            raw = await self._send_component_all_helpers()
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            logger.warning("ha_mcp_tools/helpers_list (all) failed: %r", exc)
            return None
        return cast(
            dict[str, Any],
            await shape_all_helpers_response(
                raw.get("result") or {},
                self._legacy_helper_list,
                flow_types=await helper_flow_types(self._client, refresh=True),
            ),
        )

    async def _send_component_all_helpers(self) -> dict[str, Any]:
        """Send one all-types ``ha_mcp_tools/helpers_list`` command (no type filter).

        Omitting ``helper_types`` makes the component return every storage +
        flow helper it can enumerate in a single round-trip
        (``type_filter=None`` component-side).
        """
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        return await ws.send_command(
            "ha_mcp_tools/helpers_list",
            include_flow_helpers=True,
        )

    ha_config_set_helper = tool(
        name="ha_config_set_helper",
        tags={"Helper Entities"},
        annotations=write_hints(
            "Create or Update Helper",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(
        with_auto_backup(
            domain_fn=lambda kw: f"helper_{kw.get('helper_type', 'unknown')}",
            id_fn=helper_backup_id,
            skip_fn=lambda kw: kw.get("helper_type") in SIMPLE_HELPER_TYPES,
        )(log_tool_usage(legacy_errors(SharedHelperConfigTools.ha_config_set_helper)))
    )

    async def _require_helper_type(self, helper_type: str) -> None:
        await require_helper_type(self._client, helper_type)

    async def _validate_action(
        self, action: str | None, helper_id: str | None, helper_type: str
    ) -> str:
        return await _validate_set_helper_action(
            self._client, action, helper_id, helper_type
        )

    def _schema_context(self, helper_type: str, action: str | None) -> Any:
        return _core_schema_context(self._client, helper_type, action or "create")

    _prepare_typed_params = staticmethod(_prepare_typed_params)

    async def _check_name_collision(self, helper_type: str, name: str | None) -> None:
        await _check_name_collision(self._client, helper_type, name)

    async def _validate_registry_ids(self, *args: Any, **kwargs: Any) -> None:
        await validate_registry_ids(self._client, *args, **kwargs)

    async def _flow_helper(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return cast(
            dict[str, Any], await _handle_flow_helper(self._client, *args, **kwargs)
        )

    async def _set_config_subentry(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await _handle_set_config_subentry(self._client, *args, **kwargs),
        )

    async def _attach_helper_skill(
        self, response: dict[str, Any], mandatory: bool
    ) -> None:
        _attach_helper_skill(response, mandatory)

    def _map_helper_error(
        self, error: Exception, action: str | None, helper_type: str
    ) -> Any:
        if isinstance(error, ToolError):
            raise augment_tool_error_with_skill_content(
                error, bp_warnings=None
            ) from None
        if isinstance(error, ToolFailure):
            payload = error.data
        else:
            payload = exception_to_structured_error(
                error,
                context={"action": action, "helper_type": helper_type},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify helper_id exists for update operations",
                    "Ensure required parameters are provided for the helper type",
                ],
                raise_error=False,
            )
        augment_error_dict_with_skill_content(payload, bp_warnings=None)
        raise_tool_error(payload)


def register_config_helper_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant helper configuration tools."""
    from ..transforms.component_helpers import ComponentHelperSchemaTransform

    register_tool_methods(mcp, HelperConfigTools(client))
    # Advertises Core's helper fields when the component serves helper writes.
    mcp.add_transform(ComponentHelperSchemaTransform(client))
