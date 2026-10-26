"""Remote Marketplace operations over the caller's real upstream connection.

Core dev contract reviewed 2026-10-26 at 7f6df9f7c69c2158ca7709ce1d3c457b243d7c72.
Selection probes are read-only; no mutation ever switches provider or retries.
"""

import asyncio
from typing import Any

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tool_api import ToolFailure

from .hacs_results import (
    added_data,
    download_data,
    info_data,
    refresh_data,
    remove_data,
    search_data,
)
from .hacs_values import CATEGORY_MAP, _filter_and_score_repos
from .validation import validate_identifier_not_empty


def unknown_command(error: Any) -> bool:
    return getattr(error, "code", None) == "unknown_command"


async def command(ws: Any, name: str, **values: Any) -> Any:
    response = await ws.send_command(name, **values)
    if not response.get("success"):
        error = response.get("error") or {}
        if isinstance(error, dict):
            raise HomeAssistantCommandError(
                str(error.get("message", error)), error.get("code")
            )
        raise HomeAssistantCommandError(str(error))
    return response.get("result")


async def cached_repository(
    ws: Any, domain: str, identifier: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prefer the read-only bridge, otherwise disclose the list-only projection."""
    try:
        result = await command(ws, "ha_mcp_tools/hacs_cache", repository_id=identifier)
    except HomeAssistantCommandError as error:
        if not unknown_command(error):
            raise
    else:
        if result.get("store") != domain:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Store owner changed during the read",
                status="not_started",
            )
        return result["repository"], result["cache"]
    rows = await command(ws, f"{domain}/repositories/list")
    row = next(
        (
            row
            for row in rows
            if str(row.get("id")) == identifier
            or str(row.get("full_name", "")).lower() == identifier.lower()
        ),
        None,
    )
    if row is None:
        raise ToolFailure(
            "RESOURCE_NOT_FOUND",
            f"Repository {identifier!r} not found in the loaded {domain} catalog",
            status="not_started",
        )
    return row, {
        "source": f"{domain}_catalog",
        "refreshed_from_github": False,
        "details_complete": False,
        "note": "The read-only cache bridge is unavailable. These are cached catalog fields; README and other details may be absent. The mutating repository/info endpoint was not called.",
    }


async def remote_update_target(
    ws: Any, domain: str, repository_id: str
) -> dict[str, Any] | None:
    rows = await command(ws, "config/entity_registry/list")
    owners = [
        row
        for row in rows
        if row.get("platform") == domain
        and str(row.get("unique_id")) == repository_id
        and str(row.get("entity_id", "")).startswith("update.")
        and not row.get("disabled_by")
    ]
    if len(owners) > 1:
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "Several update entities own this store repository",
            status="not_started",
        )
    if not owners:
        return None
    return {
        key: owners[0].get(key)
        for key in (
            "entity_id",
            "platform",
            "unique_id",
            "config_entry_id",
            "disabled_by",
        )
    }


async def pin_remote_update(
    ws: Any, domain: str, repository_id: str, target: dict[str, Any]
) -> None:
    if await remote_update_target(ws, domain, repository_id) != target:
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "Store update entity changed during preparation",
            status="not_started",
        )


async def try_hacs_update(
    ws: Any, repository_id: str, version: str | None
) -> dict[str, Any] | None:
    """Prefer update.install; return None only before dispatch when no entity exists."""
    target = await remote_update_target(ws, "hacs", repository_id)
    if target is None:
        return None
    before, _cache = await cached_repository(ws, "hacs", repository_id)
    values = {"entity_id": target["entity_id"]}
    if version is not None:
        values["version"] = version
    await pin_remote_update(ws, "hacs", repository_id, target)
    try:
        result = await command(
            ws,
            "call_service",
            domain="update",
            service="install",
            service_data=values,
            _wait_timeout=120.0,
        )
        await pin_remote_update(ws, "hacs", repository_id, target)
        after, cache = await cached_repository(ws, "hacs", repository_id)
        if (
            any(
                before.get(key) != after.get(key)
                for key in ("id", "full_name", "category", "domain")
            )
            or not after.get("installed")
            or (
                version
                and version
                not in (after.get("installed_version"), after.get("default_branch"))
            )
        ):
            raise RuntimeError("HACS did not confirm the targeted repository/version")
        return {
            **download_data(repository_id, after["full_name"], version, result),
            "installed_version": after.get("installed_version"),
            "pending_restart": after.get("pending_restart"),
            "active_version_verified": False,
            "cache": cache,
            "store": "hacs",
        }
    except (Exception, asyncio.CancelledError) as error:
        raise ToolFailure(
            "OUTCOME_UNKNOWN",
            f"Core update.install was dispatched for HACS; inspect the repository before another change. No retry was performed. Provider: {error}",
            status="outcome_unknown",
        ) from error


class MarketplaceRemote:
    def __init__(self, ws: Any) -> None:
        self.ws = ws

    @staticmethod
    def category(category: str | None) -> None:
        if category in {"appdaemon", "python_script"}:
            raise ToolFailure(
                "UNSUPPORTED_OPERATION",
                f"Marketplace no longer supports {category}; manage that content outside Marketplace.",
                status="not_started",
            )

    async def info(self, identifier: str) -> dict[str, Any]:
        row, cache = await cached_repository(self.ws, "marketplace", identifier)
        return {**info_data(identifier, row), "cache": cache, "store": "marketplace"}

    async def search(
        self,
        query: str,
        category: str | None,
        installed_only: bool,
        max_results: int,
        offset: int,
    ) -> dict[str, Any]:
        self.category(category)
        values = (
            {"categories": [CATEGORY_MAP.get(category, category)]} if category else {}
        )
        rows = await command(self.ws, "marketplace/repositories/list", **values)
        return {
            **search_data(
                query,
                category,
                installed_only,
                max_results,
                offset,
                _filter_and_score_repos(rows, query, installed_only),
            ),
            "store": "marketplace",
            "cache": {"source": "marketplace_catalog", "refreshed_from_github": False},
        }

    async def _admit(self, action: str) -> None:
        state = await command(self.ws, "marketplace/info")
        if state.get("startup"):
            raise ToolFailure(
                "SERVICE_UNAVAILABLE",
                "Marketplace is still starting",
                status="not_started",
            )
        if action in {"download", "add_repository"} and not state.get(
            "warning_accepted"
        ):
            raise ToolFailure(
                "AUTH_FORBIDDEN",
                "Marketplace warning_not_accepted: this administrator must accept the warning in the Marketplace UI.",
                status="not_started",
            )
        if action == "add_repository" and not state.get("github_connected"):
            raise ToolFailure(
                "AUTH_FORBIDDEN",
                "Marketplace github_not_connected: connect GitHub before adding a repository.",
                status="not_started",
            )

    async def write(
        self,
        action: str,
        identifier: str | None = None,
        version: str | None = None,
        repository: str | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        self.category(category)
        await self._admit(action)
        before, name, values, target = await self._prepare(
            action, identifier, version, repository, category
        )
        lookup = str(before["id"]) if before is not None else repository
        assert lookup is not None  # _prepare validates add_repository's identifier
        # Core authenticates every command; recheck the per-user consent just
        # before dispatch. Core's WS handler or UpdateEntity also enforces it.
        await self._admit(action)
        if target is not None:
            await pin_remote_update(self.ws, "marketplace", lookup, target)
        try:
            result = await command(self.ws, name, _wait_timeout=120.0, **values)
            if target is not None:
                await pin_remote_update(self.ws, "marketplace", lookup, target)
            after, cache = await cached_repository(self.ws, "marketplace", lookup)
            return self._finish(
                action,
                before,
                after,
                cache,
                version,
                repository,
                category,
                values,
                result,
            )
        except (Exception, asyncio.CancelledError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                f"Marketplace operation was dispatched; metadata or files may have changed. Inspect before another change; no retry was performed. Provider: {error}",
                status="outcome_unknown",
            ) from error

    async def _prepare(
        self,
        action: str,
        identifier: str | None,
        version: str | None,
        repository: str | None,
        category: str | None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any], dict[str, Any] | None]:
        target = None
        if action == "add_repository":
            repository = validate_identifier_not_empty(repository, "repository")
            category = validate_identifier_not_empty(category, "category")
            name = "marketplace/repositories/add"
            values: dict[str, Any] = {
                "repository": repository,
                "category": CATEGORY_MAP.get(category, category),
            }
            before = None
        else:
            if not identifier or not identifier.strip():
                raise ToolFailure(
                    "VALIDATION_INVALID_PARAMETER",
                    "repository_id is required",
                    status="not_started",
                )
            before, _cache = await cached_repository(self.ws, "marketplace", identifier)
            self.category(before.get("category"))
            if action == "remove" and not before.get("installed"):
                raise ToolFailure(
                    "RESOURCE_NOT_FOUND",
                    "Repository is not installed; nothing to remove",
                    status="not_started",
                )
            suffix = {
                "download": "install",
                "remove": "uninstall",
                "update_information": "refresh",
            }[action]
            name = f"marketplace/repository/{suffix}"
            values = {"repository": str(before["id"])}
            if action == "download" and version:
                values["version"] = version
            if action == "download" and before.get("installed"):
                target = await remote_update_target(
                    self.ws, "marketplace", str(before["id"])
                )
                if target is not None:
                    name = "call_service"
                    values = {
                        "domain": "update",
                        "service": "install",
                        "service_data": {"entity_id": target["entity_id"]},
                    }
                    if version:
                        values["service_data"]["version"] = version
        return before, name, values, target

    @staticmethod
    def _finish(
        action: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
        cache: dict[str, Any],
        version: str | None,
        repository: str | None,
        category: str | None,
        values: dict[str, Any],
        result: Any,
    ) -> dict[str, Any]:
        if before is not None and any(
            after.get(key) != before.get(key)
            for key in ("id", "full_name", "category", "domain")
        ):
            raise RuntimeError("Repository identity changed during the operation")
        if action == "download" and (
            not after.get("installed")
            or (version and after.get("installed_version") != version)
        ):
            raise RuntimeError(
                "Marketplace did not confirm the requested installed version"
            )
        if action == "remove" and after.get("installed"):
            raise RuntimeError("Marketplace did not confirm removal")
        if action == "add_repository":
            if after.get("category") != values["category"]:
                raise RuntimeError("Marketplace registered a different category")
            data = added_data(
                repository or after["full_name"], category or values["category"], after
            )
        else:
            identifier, repository = str(after["id"]), after["full_name"]
            if action == "download":
                data = download_data(identifier, repository, version, result)
            elif action == "remove":
                data = remove_data(identifier, repository, result)
            else:
                data = refresh_data(identifier, repository, result)
        data["message"] = data["message"].replace("HACS", "Marketplace")
        data["note"] = data.get("note", "").replace("HACS", "Marketplace")
        return {
            **data,
            "store": "marketplace",
            "installed_version": after.get("installed_version"),
            "pending_restart": after.get("pending_restart"),
            "active_version_verified": False,
            "restart_requested": False,
            "config_entries_removed": False,
            "cache": cache,
        }
