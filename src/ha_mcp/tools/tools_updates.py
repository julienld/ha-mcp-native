"""
Update management tools for Home Assistant MCP server.

This module provides tools for listing available updates, getting release notes,
and retrieving system version information.
"""

import asyncio
import logging
from typing import Any, NoReturn

import httpx

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from ..runtime.update_plans import repair_items, single_update_id, update_targets
from ..runtime.update_tools import UpdateToolsFacade
from ..runtime.update_values import (
    _categorize_update as _categorize_update,
)
from ..runtime.update_values import (
    _extract_blog_content as _extract_blog_content,
)
from ..runtime.update_values import (
    _get_monthly_versions_between as _get_monthly_versions_between,
)
from ..runtime.update_values import (
    _parse_breaking_changes_html as _parse_breaking_changes_html,
)
from ..runtime.update_values import (
    _parse_patch_breaking_changes as _parse_patch_breaking_changes,
)
from ..runtime.update_values import (
    _parse_version as _parse_version,
)
from ..runtime.update_values import (
    _strip_html as _strip_html,
)
from ..runtime.update_values import (
    _supports_release_notes as _supports_release_notes,
)
from ..runtime.update_values import (
    update_batch_result,
    update_details,
    update_listing,
)
from ..tool_api import ToolFailure
from ..utils.release_notes import (
    _fetch_core_release_notes as _fetch_core_release_notes,
)
from ..utils.release_notes import (
    _fetch_github_release_notes as _fetch_github_release_notes,
)
from ..utils.release_notes import (
    _fetch_release_data as _fetch_release_data,
)
from ..utils.release_notes import (
    _fetch_release_data_for_version as _fetch_release_data_for_version,
)
from ..utils.release_notes import (
    _try_raw_cdn as _try_raw_cdn,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import write_hints

logger = logging.getLogger(__name__)

# Batch-install category split, mirroring the HA 2026.7 Updates page: the
# "Update all" button never covers core/OS/supervisor — those are applied
# individually, on purpose.


async def _get_installed_integration_domains(client: Any) -> set[str]:
    """Get installed integration domains from config entries."""
    try:
        entries = await client._request("GET", "/config/config_entries/entry")
        if isinstance(entries, list):
            return {e.get("domain", "") for e in entries} - {""}
        return set()
    except (httpx.RequestError, ValueError, KeyError):
        return set()


class UpdateTools(UpdateToolsFacade):
    """Update management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._updates = self

    async def _list_updates(self, include_skipped: bool) -> dict[str, Any]:
        """Internal helper to list all update entities."""
        states = await self._client.get_states()

        result = update_listing(states, include_skipped)

        # Surface the MCP server's OWN update status alongside HA's updates so the
        # model is more likely to mention "you're on X, but Y is out" — this tool
        # is HA-centric, but spreading the ha-mcp self-update signal across the
        # status surfaces raises the odds an AI relays it. ``get_update_field`` is
        # best-effort and never raises; see ha_mcp.update_check for details.
        from ..update_check import get_update_field

        mcp_update = await get_update_field()
        if mcp_update is not None:
            result["ha_mcp_update"] = mcp_update

        return result

    async def _get_update_details(
        self, entity_id: str, include_release_notes: bool = False
    ) -> dict[str, Any]:
        """Internal helper to get details for a specific update entity."""
        if not entity_id.startswith("update."):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Invalid entity_id format. Must start with 'update.'",
                    context={"entity_id": entity_id},
                )
            )

        entity_state = await self._client.get_entity_state(entity_id)
        attributes = entity_state.get("attributes", {})
        latest_version = attributes.get("latest_version", "unknown")

        result = update_details(entity_state)

        # Try to fetch release notes
        release_notes, release_notes_source = await self._fetch_release_notes(
            entity_id, attributes, latest_version
        )

        if release_notes:
            result["release_notes"] = release_notes
            result["release_notes_source"] = release_notes_source
        else:
            release_url = attributes.get("release_url")
            if release_url:
                result["release_notes_hint"] = (
                    f"Release notes could not be fetched automatically. "
                    f"View them at: {release_url}"
                )

        # Include multi-version breaking change analysis for Core updates
        if include_release_notes and result.get("category") == "core":
            installed = result.get("installed_version")
            target = result.get("latest_version")
            if installed and target:
                rd_result, domains_result = await asyncio.gather(
                    _fetch_release_data(installed, target),
                    _get_installed_integration_domains(self._client),
                    return_exceptions=True,
                )
                rd = rd_result if isinstance(rd_result, dict) else {}
                domains = domains_result if isinstance(domains_result, set) else set()
                result["installed_integrations"] = sorted(domains)
                result["multi_version_release_notes"] = rd.get("release_notes", [])
                result["breaking_changes"] = {
                    "entries": rd.get("entries", []),
                    "count": rd.get("count", 0),
                    "versions_checked": rd.get("versions_checked", []),
                }

        return result

    async def _fetch_release_notes(
        self,
        entity_id: str,
        attributes: dict[str, Any],
        latest_version: str,
    ) -> tuple[Any, str | None]:
        """Fetch release notes from WebSocket, GitHub, or Core API. Returns (notes, source)."""
        # Try WebSocket update/release_notes first
        try:
            ws_result = await self._client.send_websocket_message(
                {
                    "type": "update/release_notes",
                    "entity_id": entity_id,
                }
            )
            if ws_result.get("success") and ws_result.get("result"):
                return ws_result.get("result"), "websocket"
        except Exception as ws_error:  # noqa: BLE001
            logger.debug(f"WebSocket release_notes failed for {entity_id}: {ws_error}")

        # Fallback: Try to fetch from GitHub if release_url is available
        release_url = attributes.get("release_url")
        if release_url:
            github_result = await _fetch_github_release_notes(release_url)
            if github_result:
                return github_result["notes"], github_result["source"]

        # Special handling for Home Assistant Core updates
        if "core" in entity_id.lower():
            core_result = await _fetch_core_release_notes(latest_version)
            if core_result:
                return core_result["notes"], core_result["source"]

        return None, None

    async def _resolve_update_targets(
        self,
        action: str,
        entity_ids: list[str] | str | None,
        categories: list[str] | str | None,
    ) -> list[str]:
        """Validate parameters and resolve the update entity_ids to act on."""
        ids, cats = update_targets(action, entity_ids, categories)
        if not cats:
            return ids

        listing = await self._list_updates(include_skipped=False)
        listed_categories = listing.get("categories", {})
        return [
            update["entity_id"]
            for cat in dict.fromkeys(cats)
            for update in listed_categories.get(cat, [])
            if update.get("can_install")
        ]

    async def _handle_get_action(
        self,
        entity_ids: list[str] | str | None,
        include_release_notes: bool,
    ) -> dict[str, Any]:
        """Validate the 'get' action's single-entity requirement and fetch details."""
        return await self._get_update_details(
            single_update_id(entity_ids), bool(include_release_notes)
        )

    async def _verify_targets_exist(
        self, targets: list[str]
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Split targets into (known, not-found errors) against current entity states.

        Explicit-id mode: verify the entities exist up front. HA service
        calls targeting nonexistent entities no-op silently, which would
        otherwise read as success.
        """
        states = await self._client.get_states()
        known = {s.get("entity_id") for s in states if isinstance(s, dict)}
        not_found = [
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"Update entity not found: {eid}",
                context={"entity_id": eid},
                suggestions=["Use ha_manage_updates() to list update entities"],
            )
            for eid in targets
            if eid not in known
        ]
        return [e for e in targets if e in known], not_found

    async def _call_update_service(
        self, action: str, targets: list[str], backup: bool
    ) -> tuple[list[dict[str, Any]], int]:
        """Call the update service per target, collecting per-item results."""
        results: list[dict[str, Any]] = []
        succeeded = 0
        for eid in targets:
            data: dict[str, Any] = {"entity_id": eid}
            if action == "install" and backup:
                data["backup"] = True
            try:
                await self._client.call_service("update", action, data)
                results.append({"success": True, "entity_id": eid})
                succeeded += 1
            except (HomeAssistantConnectionError, HomeAssistantAuthError):
                # Fatal transport/auth failure — the connection is gone,
                # so remaining items would fail identically. Propagate to
                # surface the root cause instead of N per-item errors.
                raise
            except Exception as e:  # noqa: BLE001
                # Batch item failure — collect, don't raise.
                results.append(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        str(e),
                        context={"entity_id": eid},
                    )
                )
        return results, succeeded

    async def _execute_batch_action(
        self,
        action: str,
        entity_ids: list[str] | str | None,
        categories: list[str] | str | None,
        backup: bool,
    ) -> dict[str, Any]:
        """Resolve targets, apply the update service, and build the batch response."""
        targets = await self._resolve_update_targets(action, entity_ids, categories)
        requested = len(targets)

        results: list[dict[str, Any]] = []
        if entity_ids:
            targets, not_found = await self._verify_targets_exist(targets)
            results.extend(not_found)

        call_results, succeeded = await self._call_update_service(
            action, targets, backup
        )
        results.extend(call_results)

        return update_batch_result(action, requested, results, succeeded)

    async def _known_repair_keys(self) -> set[tuple[str, str]] | None:
        """(domain, issue_id) of every listed Repairs issue, ignored ones included.

        None when the list cannot be read; the caller then lets Home Assistant
        judge each issue itself.
        """
        result = await self._client.send_websocket_message(
            {"type": "repairs/list_issues"}
        )
        if not result.get("success"):
            return None
        issues = (result.get("result") or {}).get("issues") or []
        return {
            (issue["domain"], issue["issue_id"])
            for issue in issues
            if isinstance(issue, dict)
            and isinstance(issue.get("domain"), str)
            and isinstance(issue.get("issue_id"), str)
        }

    async def _set_repairs_ignored(
        self, action: str, repairs: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        """Ignore or un-ignore Repairs issues, collecting per-item results."""
        items = repair_items(action, repairs)
        ignore = action == "ignore_repair"
        # list_issues omits inactive issues, which the registry keeps (ignored
        # ones included) across a restart; un-ignoring those must still reach
        # Home Assistant, so only ignore is checked against the active list.
        known = await self._known_repair_keys() if ignore else None
        results: list[dict[str, Any]] = []
        succeeded = 0
        for item in items:
            domain, issue_id = item["domain"], item["issue_id"]
            context = {"domain": domain, "issue_id": issue_id}
            if known is not None and (domain, issue_id) not in known:
                results.append(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Repairs issue not found: {domain}/{issue_id}",
                        context=context,
                        suggestions=["Use ha_get_overview() to list Repairs issues"],
                    )
                )
                continue
            reply = await self._client.send_websocket_message(
                {
                    "type": "repairs/ignore_issue",
                    "domain": domain,
                    "issue_id": issue_id,
                    "ignore": ignore,
                }
            )
            if reply.get("success"):
                results.append({"success": True, **context, "ignored": ignore})
                succeeded += 1
                continue
            results.append(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(reply.get("error") or "Home Assistant rejected the request"),
                    context=context,
                )
            )
        failed = len(items) - succeeded
        return {
            "success": failed == 0,
            "action": action,
            "requested": len(items),
            "succeeded": succeeded,
            "failed": failed,
            "results": results,
        }

    def _handle_update_error(
        self,
        e: Exception,
        action: str,
        entity_ids: list[str] | str | None,
    ) -> NoReturn:
        """Map a failure to a structured tool error; always raises."""
        error_msg = str(e)
        if (
            action == "get"
            and entity_ids
            and ("404" in error_msg or "not found" in error_msg.lower())
        ):
            eid = entity_ids if isinstance(entity_ids, str) else entity_ids[0]
            raise_tool_error(
                create_error_response(
                    ErrorCode.ENTITY_NOT_FOUND,
                    f"Update entity not found: {eid}",
                    context={"entity_id": eid},
                    suggestions=[
                        "Use ha_manage_updates() without entity_ids to see "
                        "all available updates"
                    ],
                )
            )
        logger.error(f"Failed to manage updates: {e}")
        exception_to_structured_error(
            e,
            suggestions=[
                "Check Home Assistant connection",
                "Use ha_manage_updates() to inspect available updates",
            ],
        )

    async def manage(
        self,
        action: str = "list",
        entity_ids: list[str] | str | None = None,
        categories: list[str] | str | None = None,
        include_skipped: bool = False,
        include_release_notes: bool = False,
        backup: bool = False,
        repairs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        try:
            if action == "list":
                return await self._list_updates(bool(include_skipped))

            if action == "get":
                return await self._handle_get_action(
                    entity_ids, bool(include_release_notes)
                )

            if action in ("ignore_repair", "unignore_repair"):
                return await self._set_repairs_ignored(action, repairs)

            if action not in ("install", "skip", "clear_skipped"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid action '{action}'. Must be 'list', 'get', "
                        "'install', 'skip', 'clear_skipped', 'ignore_repair', or "
                        "'unignore_repair'.",
                        context={"action": action},
                    )
                )

            return await self._execute_batch_action(
                action, entity_ids, categories, bool(backup)
            )

        except (ToolError, ToolFailure):
            raise
        except Exception as e:  # noqa: BLE001
            self._handle_update_error(e, action, entity_ids)
            return (
                None  # exception_to_structured_error always raises; explicit for CodeQL
            )

    ha_manage_updates = tool(
        name="ha_manage_updates",
        tags={"System"},
        annotations=write_hints(
            "Manage Updates", destructive=True, idempotent=False, open_world=True
        ),
    )(log_tool_usage(legacy_errors(UpdateToolsFacade.ha_manage_updates)))


def register_update_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant update management tools."""
    register_tool_methods(mcp, UpdateTools(client))
