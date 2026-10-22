"""
Trace retrieval tools for debugging Home Assistant automations and scripts.

This module provides tools for retrieving execution traces from Home Assistant
to help debug automation and script issues.
"""

import logging
from typing import Any, Literal

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.backends.legacy import legacy_errors, legacy_errors_sync
from ha_mcp.runtime.trace_results import _categorize_step_path as _categorize_step_path
from ha_mcp.runtime.trace_results import _classify_trace_steps as _classify_trace_steps
from ha_mcp.runtime.trace_results import (
    _diagnostic_suggestion as _diagnostic_suggestion,
)
from ha_mcp.runtime.trace_results import (
    _filter_trace_sections as _filter_trace_sections,
)
from ha_mcp.runtime.trace_results import (
    _format_detailed_trace as _format_detailed_trace,
)
from ha_mcp.runtime.trace_results import _format_trace_list as _format_trace_list
from ha_mcp.runtime.trace_results import (
    _populate_action_trace as _populate_action_trace,
)
from ha_mcp.runtime.trace_results import (
    _populate_condition_results as _populate_condition_results,
)
from ha_mcp.runtime.trace_results import (
    _populate_trigger_info as _populate_trigger_info,
)
from ha_mcp.runtime.trace_results import (
    _select_action_variables as _select_action_variables,
)
from ha_mcp.runtime.trace_results import trace_target as _pure_trace_target
from ha_mcp.runtime.trace_tools import TraceFacade

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    safe_progress,
)
from .tool_hints import read_only_hints
from .util_helpers import is_connection_error_message

logger = logging.getLogger(__name__)
_trace_target = legacy_errors_sync(_pure_trace_target)


class TraceTools(TraceFacade):
    """Trace retrieval tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client
        super().__init__(self._get_traces)

    ha_get_automation_traces = tool(
        name="ha_get_automation_traces",
        tags={"History & Statistics"},
        annotations=read_only_hints("Get Automation Traces", open_world=False),
    )(log_tool_usage(legacy_errors(TraceFacade.ha_get_automation_traces)))

    async def _get_traces(
        self,
        automation_id: str,
        run_id: str | None,
        limit: int,
        deduplicate: bool,
        detailed: bool,
        sections: str | None,
        offset: int,
        order: Literal["newest", "oldest"],
        ctx: Context | None,
    ) -> dict[str, Any]:
        try:
            domain, object_id = _trace_target(automation_id)

            logger.debug(
                f"ha_get_automation_traces starting: id={automation_id} "
                f"run_id={run_id or '<list>'}",
            )
            await safe_progress(
                ctx,
                progress=0,
                total=3,
                message="resolving trace target",
            )

            # Route through the shared pooled WebSocket (issue #1813) instead of
            # a dedicated connect/auth/disconnect handshake per call. Every trace
            # command below is a single request/response — no subscription or
            # streaming needs a dedicated socket — so the pooled client owns the
            # connection lifecycle. A connection-shaped failure on the fetch
            # surfaces as CONNECTION_FAILED, the same structured error the old
            # up-front connect check raised.

            # Home Assistant stores traces by unique_id, not entity_id.
            # We need to resolve entity_id -> unique_id via entity registry.
            item_id = await _resolve_trace_item_id(
                self._client, automation_id, object_id
            )

            await safe_progress(
                ctx,
                progress=1,
                total=3,
                message=f"fetching trace {'detail' if run_id else 'list'}",
            )

            if run_id:
                return await self._fetch_trace_detail(
                    domain,
                    item_id,
                    automation_id,
                    run_id,
                    deduplicate=deduplicate,
                    detailed=detailed,
                    sections=sections,
                    ctx=ctx,
                )
            return await self._fetch_trace_list(
                domain,
                item_id,
                automation_id,
                limit=limit,
                offset=offset,
                order=order,
                ctx=ctx,
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error getting traces for {automation_id}: {e}")
            exception_to_structured_error(
                e,
                context={"automation_id": automation_id},
                suggestions=[
                    "Verify the automation/script entity_id exists",
                    "Check if traces are available (automation must have run recently)",
                    "Ensure Home Assistant connection is working",
                ],
            )
            return (
                None  # exception_to_structured_error always raises; explicit for CodeQL
            )

    async def _fetch_trace_detail(
        self,
        domain: str,
        item_id: str,
        automation_id: str,
        run_id: str,
        *,
        deduplicate: bool,
        detailed: bool,
        sections: str | None,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Retrieve and format a single trace by run_id."""
        result = await self._client.send_websocket_message(
            {
                "type": "trace/get",
                "domain": domain,
                "item_id": item_id,
                "run_id": run_id,
            }
        )

        if not result.get("success"):
            _raise_trace_ws_failure(
                result.get("error", "Failed to retrieve trace"),
                {"automation_id": automation_id, "run_id": run_id},
            )

        trace_data = result.get("result", {})
        await safe_progress(ctx, progress=3, total=3, message="formatting trace")
        return _format_detailed_trace(
            automation_id,
            run_id,
            trace_data,
            deduplicate=deduplicate,
            detailed=detailed,
            sections=sections,
        )

    async def _fetch_trace_list(
        self,
        domain: str,
        item_id: str,
        automation_id: str,
        *,
        limit: int,
        offset: int,
        order: Literal["newest", "oldest"],
        ctx: Context | None,
    ) -> dict[str, Any]:
        """List recent traces, attaching diagnostics when none are stored."""
        result = await self._client.send_websocket_message(
            {
                "type": "trace/list",
                "domain": domain,
                "item_id": item_id,
            }
        )

        if not result.get("success"):
            _raise_trace_ws_failure(
                result.get("error", "Failed to list traces"),
                {"automation_id": automation_id},
            )

        traces_data = result.get("result", [])

        if not traces_data:
            await safe_progress(
                ctx,
                progress=2,
                total=3,
                message="no traces; gathering diagnostics",
            )
            diagnostics = await _gather_diagnostics(self._client, automation_id, domain)
            await safe_progress(
                ctx, progress=3, total=3, message="diagnostics complete"
            )
            return _format_trace_list(
                automation_id,
                traces_data,
                limit,
                diagnostics,
                offset=offset,
                order=order,
            )

        await safe_progress(
            ctx,
            progress=3,
            total=3,
            message=f"listed {len(traces_data)} traces",
        )
        return _format_trace_list(
            automation_id,
            traces_data,
            limit,
            offset=offset,
            order=order,
        )


def register_trace_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant trace debugging tools."""
    register_tool_methods(mcp, TraceTools(client))


def _raise_trace_ws_failure(error_msg: Any, context: dict[str, Any]) -> None:
    """Raise the structured error for a failed trace WS command.

    Since #1947 a dead transport raises out of ``send_websocket_message``
    rather than arriving here, so this path sees the failures Home Assistant
    answered with. Connection-shaped wording can still reach it (an error frame
    or a message threaded through a response body), so it stays classified as
    CONNECTION_FAILED rather than a generic SERVICE_CALL_FAILED. A genuine
    trace-command failure (unknown item, no such run) keeps its
    SERVICE_CALL_FAILED shape unchanged.
    """
    if is_connection_error_message(error_msg):
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                str(error_msg),
                context=context,
                suggestions=[
                    "Home Assistant may be restarting or unreachable — retry shortly",
                    "Check the connection to Home Assistant",
                ],
            )
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            error_msg,
            context=context,
        )
    )


async def _resolve_trace_item_id(
    client: Any, entity_id: str, fallback_object_id: str
) -> str:
    """
    Resolve entity_id to the unique_id used for trace storage.

    Home Assistant stores traces using the automation/script's unique_id,
    not the entity_id. This function looks up the unique_id from the
    entity registry and falls back to object_id if not found.

    Args:
        client: Pooled REST client exposing ``send_websocket_message``
        entity_id: Full entity_id (e.g., 'automation.morning_routine')
        fallback_object_id: Object ID to use if unique_id lookup fails

    Returns:
        The unique_id for trace lookup, or fallback_object_id
    """
    try:
        # Query entity registry to get unique_id. Best-effort: the pooled client
        # collapses a failure into ``{"success": False, ...}`` (or, for a
        # programming bug, raises) — either way we fall back to object_id and let
        # the subsequent trace fetch surface the real error.
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )

        if result.get("success") and result.get("result"):
            unique_id = result["result"].get("unique_id")
            if unique_id:
                logger.debug(f"Resolved {entity_id} to unique_id: {unique_id}")
                return str(unique_id)

        # Fallback to object_id if no unique_id found
        logger.debug(
            f"No unique_id found for {entity_id}, using object_id: {fallback_object_id}"
        )
        return fallback_object_id

    except Exception as e:  # noqa: BLE001
        # On any error, fall back to object_id
        logger.warning(
            f"Failed to resolve unique_id for {entity_id}: {e}, "
            f"using object_id: {fallback_object_id}"
        )
        return fallback_object_id


async def _gather_diagnostics(
    client: Any,
    automation_id: str,
    domain: str,
) -> dict[str, Any]:
    """
    Gather diagnostic information when traces are empty.

    This helps users understand why there are no traces available for
    an automation or script.

    Args:
        client: Pooled REST client (REST reads + ``send_websocket_message``)
        automation_id: Full entity_id (e.g., 'automation.motion_light')
        domain: Either 'automation' or 'script'

    Returns:
        Dictionary containing diagnostic information:
        - automation_exists: Whether the entity exists
        - automation_enabled: Whether the automation is enabled (on/off state)
        - trace_storage_enabled: Whether trace storage is enabled for this item
        - last_triggered: Last trigger timestamp if available
        - suggestion: Helpful hint based on the diagnostics
    """
    diagnostics: dict[str, Any] = {
        "automation_exists": False,
        "automation_enabled": False,
        "trace_storage_enabled": True,  # Default assumption
        "last_triggered": None,
        "suggestion": "",
    }

    try:
        # Get entity state to check existence and enabled status
        entity_state = await client.get_entity_state(automation_id)

        if entity_state:
            diagnostics["automation_exists"] = True

            # Check if enabled (state is 'on' for automations, 'off' is disabled)
            state = entity_state.get("state", "unknown")
            diagnostics["automation_enabled"] = state == "on"

            # Get last_triggered from attributes
            attributes = entity_state.get("attributes", {})
            last_triggered = attributes.get("last_triggered")
            if last_triggered:
                diagnostics["last_triggered"] = last_triggered

            # Check if tracing is stored - only for automations
            # (scripts always store traces when enabled)
            if domain == "automation":
                diagnostics["trace_storage_enabled"] = await _is_trace_storage_enabled(
                    client, automation_id, attributes
                )

            diagnostics["suggestion"] = _diagnostic_suggestion(diagnostics, domain)

    except Exception as e:  # noqa: BLE001
        # Entity doesn't exist or error occurred
        logger.debug(f"Error getting entity state for diagnostics: {e}")
        diagnostics["suggestion"] = (
            f"Could not find {automation_id}. "
            "Verify the entity_id is correct using ha_search()."
        )

    return diagnostics


async def _is_trace_storage_enabled(
    client: Any,
    automation_id: str,
    attributes: dict[str, Any],
) -> bool:
    """Return whether stored_traces is enabled for an automation (defaults True)."""
    try:
        unique_id = attributes.get("id")
        if unique_id:
            config_result = await client.send_websocket_message(
                {"type": "automation/config", "entity_id": automation_id}
            )
            if config_result.get("success"):
                config = config_result.get("result", {})
                # stored_traces defaults to True if not specified
                stored_traces = config.get("stored_traces")
                if stored_traces is not None and stored_traces <= 0:
                    return False
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Could not get automation config: {e}")
    return True
