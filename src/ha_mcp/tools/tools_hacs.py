"""
HACS (Home Assistant Community Store) integration tools for Home Assistant MCP server.

This module provides tools to interact with HACS via the WebSocket API, enabling AI agents
to discover custom integrations, Lovelace cards, themes, and more.
"""

import functools
import inspect
import logging
from typing import Any

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.tool_api import ToolFailure

from ..backends.legacy import legacy_errors
from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import HomeAssistantWebSocketClient
from ..errors import ErrorCode, create_error_response
from ..runtime.hacs_remote import (
    MarketplaceRemote,
    cached_repository,
    command,
    try_hacs_update,
    unknown_command,
)
from ..runtime.hacs_results import (
    added_data,
    download_data,
    info_data,
    refresh_data,
    remove_data,
    search_data,
)
from ..runtime.hacs_tools import HacsFacade
from .hacs_registration import (
    CATEGORY_MAP,
    HACS_ADD_REGISTRATION_TIMEOUT,
    HACS_REFRESH_TIMEOUT,
    HACS_RESOLVE_REGISTRATION_TIMEOUT,
    _filter_and_score_repos,
    send_hacs_repository_refresh,
    wait_for_repo_registration,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    safe_progress,
    validate_identifier_not_empty,
)
from .response_helpers import add_timezone_metadata
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)


async def _assert_hacs_available(ws_client: HomeAssistantWebSocketClient) -> str:
    """Select a loaded store with read-only probes before any mutation."""
    available = []
    for domain in ("hacs", "marketplace"):
        try:
            await command(ws_client, f"{domain}/info")
        except HomeAssistantCommandError as error:
            if unknown_command(error) or (
                domain == "marketplace" and error.code == "not_loaded"
            ):
                continue
            raise
        else:
            available.append(domain)
    if len(available) > 1:
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "Both HACS and Marketplace are loaded; resolve their ownership before using store tools.",
            status="not_started",
        )
    if not available:
        raise ToolFailure(
            "COMPONENT_NOT_INSTALLED",
            "Neither HACS nor Marketplace is loaded.",
            status="not_started",
        )
    return available[0]


def _hacs_legacy_errors(function: Any) -> Any:
    """Retain historical action context at the legacy SDK boundary."""

    @functools.wraps(function)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        try:
            return await function(*args, **kwargs)
        except (ToolError, ToolFailure):
            raise
        except Exception as error:
            bound = inspect.signature(function).bind_partial(*args, **kwargs)
            suggestions = ["Verify HACS is installed: https://hacs.xyz/"]
            if function.__name__ == "ha_get_hacs_info":
                suggestions.extend(
                    [
                        "For action='search', try a simpler query or a valid category",
                        "For action='info', pass a valid repository_id (numeric ID or 'owner/repo')",
                    ]
                )
            else:
                suggestions.extend(
                    [
                        "For action='download', 'remove', or 'update_information', pass a valid repository_id (use ha_get_hacs_info(action='search') to find it)",
                        "For action='add_repository', use 'owner/repo' format and a matching category",
                    ]
                )
            exception_to_structured_error(
                error,
                context={
                    "tool": function.__name__,
                    "action": bound.arguments.get("action"),
                },
                suggestions=suggestions,
            )
            raise

    return legacy_errors(invoke)


class HacsTools:
    """HACS integration tools for Home Assistant.

    Two action-based tools split along the read/write boundary so the
    read path keeps ``readOnlyHint`` and is never flagged ``destructive``:

    - ``ha_get_hacs_info`` (read): ``search`` the store / ``info`` for one repo.
    - ``ha_manage_hacs`` (write): ``download`` install/update / ``remove`` / ``add_repository`` / ``update_information`` refresh.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.hacs = self

    async def _get_hacs_client(
        self,
    ) -> HomeAssistantWebSocketClient | MarketplaceRemote:
        """Check HACS on the current request's connection, including OAuth users."""
        from ..client.websocket_client import get_websocket_client

        ws_client = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=self._client.verify_ssl,
        )
        if await _assert_hacs_available(ws_client) == "marketplace":
            return MarketplaceRemote(ws_client)
        return ws_client

    async def _store_result(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, **await add_timezone_metadata(self._client, data)}

    # --- Private action handlers ------------------------------------------
    # The public tools above are thin dispatchers; each handler raises a
    # structured ToolError on failure, caught by the dispatcher's wrapper.

    async def _hacs_search(
        self,
        query: str,
        category: str | None,
        installed_only: bool,
        max_results: int,
        offset: int,
        ctx: Context | None,
    ) -> dict[str, Any]:
        logger.debug(
            "ha_get_hacs_info search starting: "
            f"category={category} installed_only={installed_only}",
        )
        await safe_progress(
            ctx, progress=0, total=3, message="checking HACS availability"
        )

        ws_client = await self._get_hacs_client()

        if isinstance(ws_client, MarketplaceRemote):
            return await self._store_result(
                await ws_client.search(
                    query, category, installed_only, max_results, offset
                )
            )

        # Build command parameters - map user-friendly category to HACS internal name
        kwargs_cmd: dict[str, Any] = {}
        if category:
            hacs_category = CATEGORY_MAP.get(category, category)
            kwargs_cmd["categories"] = [hacs_category]

        await safe_progress(
            ctx, progress=1, total=3, message="fetching HACS repository list"
        )

        response = await ws_client.send_command("hacs/repositories/list", **kwargs_cmd)

        if not response.get("success"):
            exception_to_structured_error(
                Exception(f"HACS search request failed: {response}"),
                context={
                    "command": "hacs/repositories/list",
                    "query": query,
                    "category": category,
                },
                raise_error=True,
            )

        all_repositories = response.get("result", [])
        await safe_progress(
            ctx,
            progress=2,
            total=3,
            message=f"filtering {len(all_repositories)} repositories",
        )
        matches = _filter_and_score_repos(all_repositories, query, installed_only)
        await safe_progress(
            ctx, progress=3, total=3, message=f"matched {len(matches)} repositories"
        )

        wrapped = await add_timezone_metadata(
            self._client,
            search_data(query, category, installed_only, max_results, offset, matches),
        )
        return {"success": True, **wrapped}

    async def _hacs_info(self, repository_id: str) -> dict[str, Any]:
        ws_client = await self._get_hacs_client()
        if isinstance(ws_client, MarketplaceRemote):
            return await self._store_result(await ws_client.info(repository_id))
        try:
            result, cache = await cached_repository(ws_client, "hacs", repository_id)
        except HomeAssistantCommandError as cmd_err:
            exception_to_structured_error(
                cmd_err,
                context={
                    "command": "ha_mcp_tools/hacs_cache",
                    "repository_id": repository_id,
                },
                raise_error=True,
            )

        wrapped = await add_timezone_metadata(
            self._client,
            {**info_data(repository_id, result), "cache": cache},
        )
        return {"success": True, **wrapped}

    async def _hacs_download(
        self, repository_id: str | None, version: str | None
    ) -> dict[str, Any]:
        # Empty/whitespace repository_id would either be passed straight
        # into ``_resolve_hacs_repo_id`` (which has no empty-check and
        # would fall through to a HACS lookup miss) or — for a numeric
        # candidate — reach ``hacs/repository/download`` with an empty
        # repository field. Same destructive-WS-call class as
        # ``ha_manage_app``: guard up-front so the caller learns the
        # identifier was unusable before any backend call.
        repository_id = validate_identifier_not_empty(
            repository_id,
            "repository_id",
            suggestions=[
                "Use ha_get_hacs_info(action='search') to find valid repository IDs",
                "Or pass a GitHub path like 'owner/repo' to install by name",
            ],
        )
        ws_client = await self._get_hacs_client()

        if isinstance(ws_client, MarketplaceRemote):
            return await self._store_result(
                await ws_client.write("download", repository_id, version)
            )

        # Resolve GitHub path to numeric ID if needed
        actual_id, repo_name = await _resolve_hacs_repo_id(ws_client, repository_id)

        if updated := await try_hacs_update(ws_client, actual_id, version):
            return await self._store_result(updated)

        # Build download command parameters
        download_kwargs: dict[str, Any] = {"repository": actual_id}
        if version:
            download_kwargs["version"] = version

        # Download/install the repository. Same 60 s budget as remove: HACS
        # refreshes from GitHub before acting, and a slow GitHub past the
        # 30 s default reports a false failure for work that completes.
        try:
            response = await ws_client.send_command(
                "hacs/repository/download", _wait_timeout=60.0, **download_kwargs
            )
        except HomeAssistantCommandTimeout as timeout_err:
            # Same false-failure shape remove documents: HACS finishes the
            # download regardless, so name the real 60 s budget (the generic
            # classifier would claim 30) and say the outcome is unknown
            # instead of inviting a blind retry.
            exception_to_structured_error(
                timeout_err,
                context={
                    "command": "hacs/repository/download",
                    "repository_id": repository_id,
                    "version": version,
                    "timeout_seconds": 60.0,
                },
                suggestions=[
                    "The download may still have completed on the HACS side — "
                    "verify with ha_get_hacs_info(action='info', "
                    f"repository_id='{actual_id}') before retrying",
                ],
                raise_error=True,
            )
        except HomeAssistantCommandError as cmd_err:
            exception_to_structured_error(
                cmd_err,
                context={
                    "command": "hacs/repository/download",
                    "repository_id": repository_id,
                    "version": version,
                    "timeout_seconds": 60.0,
                },
                raise_error=True,
            )

        if not response.get("success"):
            exception_to_structured_error(
                Exception(f"HACS download request failed: {response}"),
                context={
                    "command": "hacs/repository/download",
                    "repository_id": repository_id,
                    "version": version,
                },
                raise_error=True,
            )

        result = response.get("result", {})

        wrapped = await add_timezone_metadata(
            self._client,
            download_data(actual_id, repo_name, version, result),
        )
        return {"success": True, **wrapped}

    async def _hacs_remove(self, repository_id: str | None) -> dict[str, Any]:
        # Same up-front guard as ``_hacs_download``: an empty identifier
        # must fail before any destructive WS call.
        repository_id = validate_identifier_not_empty(
            repository_id,
            "repository_id",
            suggestions=[
                "Use ha_get_hacs_info(action='search', installed_only=True) "
                + "to list downloaded repositories",
                "Or pass a GitHub path like 'owner/repo' to remove by name",
            ],
        )
        ws_client = await self._get_hacs_client()

        if isinstance(ws_client, MarketplaceRemote):
            return await self._store_result(
                await ws_client.write("remove", repository_id)
            )

        actual_id, repo_name = await _resolve_hacs_repo_id(ws_client, repository_id)

        # The info handler itself refreshes and clears metadata; use only the
        # cache bridge/catalog before deciding whether files can be removed.
        info_result, _cache = await cached_repository(ws_client, "hacs", actual_id)
        repo_name = info_result.get("full_name") or info_result.get("name") or repo_name
        if not info_result.get("installed"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Repository '{repo_name}' is not downloaded — nothing to remove.",
                    suggestions=[
                        "Use ha_get_hacs_info(action='search', "
                        + "installed_only=True) to list downloaded repositories",
                    ],
                    context={"repository_id": actual_id, "repository": repo_name},
                )
            )

        # HACS refreshes the repository from GitHub (forced) before
        # uninstalling, so a slow or rate-limited GitHub can push the
        # round-trip past the 30 s default — and a timeout here is a FALSE
        # failure that invites a destructive retry, because HACS finishes
        # the uninstall regardless. Give it the same 60 s the add path got
        # (#1623) and say the outcome is unknown when it still trips.
        try:
            response = await ws_client.send_command(
                "hacs/repository/remove", repository=actual_id, _wait_timeout=60.0
            )
        except HomeAssistantCommandTimeout as timeout_err:
            exception_to_structured_error(
                timeout_err,
                context={
                    "command": "hacs/repository/remove",
                    "repository_id": repository_id,
                    "timeout_seconds": 60.0,
                },
                suggestions=[
                    "The removal may still have completed on the HACS side — "
                    "verify with ha_get_hacs_info(action='info', "
                    f"repository_id='{actual_id}') before retrying",
                ],
                raise_error=True,
            )
        except HomeAssistantCommandError as cmd_err:
            # The WS client RAISES on a failed "result" response (it never
            # returns success=False), so this — not the dict branch below,
            # which serves stubbed/alternative clients — is where a real
            # HACS remove failure lands; keep the command context attached.
            exception_to_structured_error(
                cmd_err,
                context={
                    "command": "hacs/repository/remove",
                    "repository_id": repository_id,
                    "timeout_seconds": 60.0,
                },
                raise_error=True,
            )

        if not response.get("success"):
            exception_to_structured_error(
                Exception(f"HACS remove request failed: {response}"),
                context={
                    "command": "hacs/repository/remove",
                    "repository_id": repository_id,
                },
                raise_error=True,
            )

        wrapped = await add_timezone_metadata(
            self._client,
            remove_data(actual_id, repo_name, response.get("result", {})),
        )
        return {"success": True, **wrapped}

    async def _hacs_update_information(
        self, repository_id: str | None
    ) -> dict[str, Any]:
        # Same up-front guard as ``_hacs_download``: an empty identifier must
        # fail before any backend call.
        repository_id = validate_identifier_not_empty(
            repository_id,
            "repository_id",
            suggestions=[
                "Use ha_get_hacs_info(action='search') to find valid repository IDs",
                "Or pass a GitHub path like 'owner/repo' to refresh by name",
            ],
        )
        ws_client = await self._get_hacs_client()

        if isinstance(ws_client, MarketplaceRemote):
            return await self._store_result(
                await ws_client.write("update_information", repository_id)
            )

        actual_id, repo_name = await _resolve_hacs_repo_id(ws_client, repository_id)

        # Without the explicit timeout context, the generic classifier reports
        # its 30 s default — a false claim against this call's 60 s budget.
        # The refresh is idempotent, so unlike remove no unknown-outcome
        # warning is needed: retrying a completed refresh is harmless.
        try:
            response = await send_hacs_repository_refresh(ws_client, actual_id)
        except HomeAssistantCommandTimeout as timeout_err:
            exception_to_structured_error(
                timeout_err,
                context={
                    "command": "hacs/repository/refresh",
                    "repository_id": repository_id,
                    "timeout_seconds": HACS_REFRESH_TIMEOUT,
                },
                suggestions=[
                    "GitHub may be slow; the refresh is safe to retry",
                ],
                raise_error=True,
            )
        except HomeAssistantCommandError as cmd_err:
            # The WS client RAISES on a failed result frame (it never returns
            # success=False), so this — not the dict branch below, which
            # serves stubbed clients — is where a real HACS refresh failure
            # lands. ``timeout_seconds`` rides along because a HACS-side
            # message containing "timeout" routes to the by-message timeout
            # branch, which would otherwise claim 30 s against this budget.
            exception_to_structured_error(
                cmd_err,
                context={
                    "command": "hacs/repository/refresh",
                    "repository_id": repository_id,
                    "timeout_seconds": HACS_REFRESH_TIMEOUT,
                },
                raise_error=True,
            )

        if not response.get("success"):
            exception_to_structured_error(
                Exception(f"HACS refresh request failed: {response}"),
                context={
                    "command": "hacs/repository/refresh",
                    "repository_id": repository_id,
                },
                raise_error=True,
            )

        wrapped = await add_timezone_metadata(
            self._client,
            refresh_data(actual_id, repo_name, response.get("result", {})),
        )
        return {"success": True, **wrapped}

    async def _hacs_add_repository(
        self, repository: str, category: str
    ) -> dict[str, Any]:
        ws_client = await self._get_hacs_client()

        if isinstance(ws_client, MarketplaceRemote):
            return await self._store_result(
                await ws_client.write(
                    "add_repository", repository=repository, category=category
                )
            )

        # Validate repository format
        if "/" not in repository:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Invalid repository format. Must be 'owner/repo'",
                    suggestions=[
                        "Use format: 'owner/repo' (e.g., 'hacs/integration')",
                        "Check the repository exists on GitHub",
                    ],
                )
            )

        # Map user-friendly category to HACS internal name
        hacs_category = CATEGORY_MAP.get(category, category)

        response = await ws_client.send_command(
            "hacs/repositories/add",
            repository=repository,
            category=hacs_category,
        )

        if not response.get("success"):
            exception_to_structured_error(
                Exception(f"HACS add repository request failed: {response}"),
                context={
                    "command": "hacs/repositories/add",
                    "repository": repository,
                    "category": category,
                },
                raise_error=True,
            )

        # HACS' add command returns ``success`` on acceptance but registers the
        # repository asynchronously and returns no id in the ack. Confirm it
        # actually registered (mirroring the download path) — an accepted-but-
        # never-registered add (archived repo, bad structure, wrong category)
        # would otherwise report a misleading "Successfully added".
        repo = await wait_for_repo_registration(
            ws_client, repository, timeout=HACS_ADD_REGISTRATION_TIMEOUT
        )
        if repo is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"HACS accepted the request but '{repository}' did not "
                    "register as a custom repository.",
                    suggestions=[
                        "Verify the repository exists and follows HACS structure (e.g. has hacs.json)",
                        "Check that the repository is not archived",
                        "Ensure the category matches the repository type",
                    ],
                )
            )

        wrapped = await add_timezone_metadata(
            self._client,
            added_data(repository, category, repo),
        )
        return {"success": True, **wrapped}

    ha_get_hacs_info = tool(
        name="ha_get_hacs_info",
        tags={"HACS"},
        annotations=read_only_hints("Get HACS Info", open_world=True),
    )(log_tool_usage(_hacs_legacy_errors(HacsFacade.ha_get_hacs_info)))
    ha_manage_hacs = tool(
        name="ha_manage_hacs",
        tags={"HACS"},
        annotations=write_hints(
            "Manage HACS", destructive=True, idempotent=False, open_world=True
        ),
    )(log_tool_usage(_hacs_legacy_errors(HacsFacade.ha_manage_hacs)))


def register_hacs_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register HACS integration tools with the MCP server."""
    register_tool_methods(mcp, HacsTools(client))


async def _resolve_hacs_repo_id(ws_client: Any, repository_id: str) -> tuple[str, str]:
    """Resolve a GitHub path (owner/repo) to a HACS numeric repository ID and name.

    Returns (numeric_id, display_name). If repository_id is already numeric,
    returns (repository_id, repository_id).

    For GitHub-path identifiers, this uses the HACS dispatch-signal
    waiter so that a caller running immediately after
    ``ha_manage_hacs(action="add_repository")`` doesn't race against
    HACS' internal registration.
    """
    if "/" not in repository_id:
        return repository_id, repository_id

    repo = await wait_for_repo_registration(
        ws_client, repository_id, timeout=HACS_RESOLVE_REGISTRATION_TIMEOUT
    )

    if repo is not None:
        return str(repo.get("id")), repo.get("name") or repository_id

    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"Repository '{repository_id}' not found in HACS",
            suggestions=[
                "Use ha_get_hacs_info(action='search') to find the repository",
                "Check the repository name is correct (case-insensitive)",
                "The repository may need to be added to HACS first",
            ],
        )
    )
    return None  # unreachable: raise_tool_error always raises


__all__ = [
    "HacsTools",
    "register_hacs_tools",
]
