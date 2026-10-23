"""Canonical dashboard orchestration over live Core owners and pure edit logic."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.dashboard_errors import raise_dashboard_edit_error
from ha_mcp.runtime.dashboard_search import (
    _SearchCriteria,
    _unread_dashboards_warning,
    build_search_result,
)
from ha_mcp.runtime.dashboard_validation import (
    reject_malformed_dashboard_lists,
    reject_malformed_dashboard_patch,
)
from ha_mcp.runtime.dashboard_values import (
    _attach_dashboard_render_paths,
    _large_dashboard_replacement_warning,
    apply_dashboard_python_transform,
    edit_dashboard_result,
    get_dashboard_result,
    set_dashboard_result,
    validate_strategy_dashboard_replacement,
)
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.runtime.validation import validate_identifier_not_empty
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import compute_config_hash
from ha_mcp.utils.dashboard_patch import apply_dashboard_patch
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .dashboard_advice import DashboardAdvice
from .dashboard_storage import DashboardState, DashboardStorage
from .registry_access import admit_registry_write, authorize_actor


@dataclass(slots=True)
class NativeDashboards:
    manager: Any
    actor: CallContext
    storage: DashboardStorage = field(init=False)
    advice: DashboardAdvice = field(init=False)

    def __post_init__(self) -> None:
        self.storage = DashboardStorage(
            self.manager.hass, self.actor, authorize=self.manager.backend.authorize
        )
        self.advice = DashboardAdvice(self.manager, self.actor)

    async def _project(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        result = await await_owned_io(
            self.manager.hass.async_add_executor_job(partial(function, *args, **kwargs))
        )
        await authorize_actor(self.actor, self.manager.backend.authorize)
        return result

    async def get(
        self,
        url_path: str | None = None,
        list_only: bool = False,
        force_reload: bool = False,
        entity_id: str | None = None,
        card_type: str | None = None,
        heading: str | None = None,
        include_config: bool = False,
        include_screenshot: bool = False,
        view_path: str | None = None,
        query: str | None = None,
        describe: bool = False,
    ) -> Any:
        try:
            await authorize_actor(self.actor, self.manager.backend.authorize)
            if describe:
                return await self.advice.describe(card_type)
            search = any(
                value is not None for value in (entity_id, card_type, heading, query)
            )
            if list_only and not search:
                rows = self.storage.metadata()
                result: dict[str, Any] = {
                    "success": True,
                    "action": "list",
                    "dashboards": rows,
                    "count": len(rows),
                }
            else:
                if url_path is not None:
                    validate_identifier_not_empty(url_path, "url_path")
                if search:
                    result = await self._search(
                        url_path,
                        _SearchCriteria(
                            entity_id=entity_id,
                            card_type=card_type,
                            heading=heading,
                            query=query,
                        ),
                        include_config,
                    )
                    if list_only:
                        result.setdefault("warnings", []).append(
                            "list_only was ignored because search parameters were given."
                        )
                else:
                    return await self._get_config(
                        url_path, force_reload, view_path, include_screenshot
                    )
            if include_screenshot or view_path is not None:
                result.setdefault("warnings", []).append(
                    f"include_screenshot, view_path, and screenshot render options are ignored in {result['action']} mode; call ha_config_get_dashboard with a url_path (and no search criteria) to scope to a view or get a screenshot."
                )
            return result
        except Exception as error:
            raise self.storage.failure(error) from error

    async def _get_config(
        self,
        identifier: str | None,
        force: bool,
        view_path: str | None,
        screenshot: bool,
    ) -> Any:
        before = await self.storage.read(identifier, force=force)
        if before.owner is None or before.config is None:
            raise_dashboard_edit_error(
                before.url_path,
                "not_found",
                "Dashboard has no saved config",
                False,
                "get",
            )
        path = (
            identifier
            if identifier in (None, "default", "lovelace")
            else before.url_path
        )
        result = await self._project(
            get_dashboard_result,
            path,
            before.config,
            original_url_path=identifier,
            view_path=view_path,
        )
        return await self.advice.screenshot(
            result,
            before.url_path,
            before.config,
            view_path,
            requested=screenshot,
            after_write=False,
        )

    async def _search(
        self, identifier: str | None, criteria: _SearchCriteria, include_config: bool
    ) -> dict[str, Any]:
        if criteria.is_empty() or (
            not criteria.can_match_cards() and not criteria.can_match_badges()
        ):
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Search needs compatible, non-blank query, entity_id, card_type or heading",
                    context={"action": "search"},
                )
            )
        notes: list[str] = []
        docs: list[dict[str, Any]] = []
        suppressed = None
        path = None
        if identifier is not None:
            before = await self.storage.read(identifier, force=True)
            if before.owner is None or before.config is None:
                raise_dashboard_edit_error(
                    before.url_path,
                    "not_found",
                    "Dashboard has no saved config",
                    False,
                    "search",
                )
            path = before.url_path
            docs.append({"url_path": path, "config": before.config})
            if before.owner.mode != "storage":
                suppressed = f"Matched values and card config bodies were withheld: dashboard {path!r} is not provably storage-mode (a YAML dashboard's config can carry resolved !secret values). Match locations and field names are reported."
        else:
            docs, notes = await self._search_docs()
        result: dict[str, Any] = await self._project(
            build_search_result,
            docs,
            criteria=criteria,
            include_config=include_config,
            url_path=path,
            config_suppressed_note=suppressed,
        )
        if identifier is not None and identifier not in ("default", path):
            result["resolved_from"] = identifier
        if notes:
            result.setdefault("warnings", []).extend(notes)
        return result

    async def _search_docs(self) -> tuple[list[dict[str, Any]], list[str]]:
        docs, failures = [], []
        for key, owner in tuple(self.storage._dashboards().items()):
            if owner.mode != "storage" or (
                key is None and "lovelace" in self.storage._dashboards()
            ):
                continue
            try:
                before = await self.storage.read(key, force=True)
            except Exception as error:
                if isinstance(error, ToolFailure) and error.code == "AUTH_FORBIDDEN":
                    raise
                failures.append(key or "default")
                continue
            if isinstance(before.config, dict):
                docs.append({"url_path": before.url_path, "config": before.config})
            elif before.config is not None:
                failures.append(key or "default")
        return docs, _unread_dashboards_warning(failures)

    async def _capture(self, before: DashboardState, source: str) -> list[str]:
        if before.snapshot is None:
            return []
        result = await self.manager.snapshots.dashboard_backup(
            {
                "source_tool": source,
                "url_path": before.url_path,
                "current_config": before.snapshot,
            },
            self.actor,
        )
        return list(result.get("warnings") or []) if result else []

    @staticmethod
    def _validate_request(
        url_path: str,
        config: Any,
        python_transform: str | None,
        patch: Any,
        config_hash: str | None,
        metadata: dict[str, Any],
        return_screenshot: bool,
        view_path: str | None,
    ) -> str:
        validate_identifier_not_empty(url_path, "url_path")
        action = (
            "patch"
            if patch is not None
            else "python_transform"
            if python_transform is not None
            else "set"
        )
        if sum(value is not None for value in (config, python_transform, patch)) > 1:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "config, python_transform and patch are mutually exclusive and cannot be used simultaneously",
                )
            )
        if action != "set" and config_hash is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"config_hash is required for {action}",
                    suggestions=["Read the dashboard first and use its config_hash"],
                )
            )
        if patch is not None and metadata:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "patch cannot be combined with dashboard metadata (title, icon, require_admin, show_in_sidebar)",
                )
            )
        if return_screenshot and view_path is not None and not view_path.strip():
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER, "view_path cannot be empty."
                )
            )
        return action

    async def _candidate(
        self,
        before: DashboardState,
        config: Any,
        python_transform: str | None,
        patch: Any,
        config_hash: str | None,
        action: str,
    ) -> dict[str, Any] | None:
        if action == "set" and config is None:
            return None
        if (
            before.owner is not None
            and config_hash is not None
            and (
                before.config is None
                or compute_config_hash(before.config) != config_hash
            )
        ):
            raise_dashboard_edit_error(
                before.url_path,
                "conflict",
                "Dashboard modified since last read (conflict)",
                False,
                action,
                hash_supplied=True,
            )
        if action != "set" and before.config is None:
            raise_dashboard_edit_error(
                before.url_path,
                "not_found",
                "Dashboard has no saved config",
                False,
                action,
            )
        if python_transform is not None:
            transformed: dict[str, Any] = await self._project(
                apply_dashboard_python_transform,
                before.url_path,
                python_transform,
                before.config,
            )
            return transformed
        if patch is not None:
            reject_malformed_dashboard_patch(patch, before.url_path)
            try:
                candidate = await self._project(
                    apply_dashboard_patch, before.config, patch
                )
            except ValueError as error:
                raise_dashboard_edit_error(
                    before.url_path, "validation_failed", str(error), False, action
                )
        else:
            candidate = copy.deepcopy(config)
        if not isinstance(candidate, dict):
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Config parameter must be a dict/object",
                )
            )
        validate_strategy_dashboard_replacement(
            before.url_path,
            was_strategy_dashboard=before.config is not None
            and "strategy" in before.config,
            replacement_config=candidate,
            action=action,
        )
        reject_malformed_dashboard_lists(
            candidate,
            before.url_path,
            source="patch" if action == "patch" else "config",
        )
        return candidate

    async def set(
        self,
        url_path: str,
        config: dict[str, Any] | None = None,
        python_transform: str | None = None,
        config_hash: str | None = None,
        title: str | None = None,
        icon: str | None = None,
        require_admin: bool | None = None,
        show_in_sidebar: bool | None = None,
        MandatoryBPS: bool = True,
        BestPracticeKey: str | None = None,
        return_screenshot: bool = False,
        view_path: str | None = None,
        patch: list[dict[str, Any]] | None = None,
    ) -> Any:
        metadata: dict[str, Any] = {
            key: value
            for key, value in {
                "title": title,
                "icon": icon,
                "require_admin": require_admin,
                "show_in_sidebar": show_in_sidebar,
            }.items()
            if value is not None
        }
        if "icon" in metadata:
            metadata["icon"] = metadata["icon"] or None
        try:
            action = self._validate_request(
                url_path,
                config,
                python_transform,
                patch,
                config_hash,
                metadata,
                return_screenshot,
                view_path,
            )
            await admit_registry_write(self.actor, self.manager.backend.authorize)
            canonical, _owner = self.storage.resolve(url_path)
            async with registry_update_lock("dashboard", canonical):
                before = await self.storage.read(canonical)
                candidate = await self._candidate(
                    before, config, python_transform, patch, config_hash, action
                )
                result, after = await self._apply(
                    before, candidate, metadata, action, python_transform, url_path
                )
                if after.config is not None:
                    if advice := await self.advice.warnings(after.config):
                        result.setdefault("warnings", []).extend(advice)
                await self.advice.attach_skill(result, MandatoryBPS)
                await authorize_actor(self.actor, self.manager.backend.authorize)
                return await self.advice.screenshot(
                    result,
                    after.url_path,
                    after.config,
                    view_path,
                    requested=return_screenshot,
                    after_write=True,
                )
        except Exception as error:
            raise self.storage.failure(error) from error

    async def _apply(
        self,
        before: DashboardState,
        candidate: dict[str, Any] | None,
        metadata: dict[str, Any],
        action: str,
        python_transform: str | None,
        identifier: str,
    ) -> tuple[dict[str, Any], DashboardState]:
        resolved = (
            identifier if identifier not in (before.url_path, "default") else None
        )
        if (
            action == "patch"
            and candidate is not None
            and before.config is not None
            and compute_config_hash(candidate) == compute_config_hash(before.config)
        ):
            self.storage.check(before)
            return edit_dashboard_result(
                before.url_path,
                {
                    "config": before.config,
                    "config_hash": compute_config_hash(before.config),
                    "write_committed": False,
                    "post_write_verified": True,
                    "unchanged": True,
                },
                action,
                resolved,
            ), before
        if before.owner is not None and candidate is None and not metadata:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "No dashboard changes were requested",
                )
            )
        if before.owner is not None:
            self.storage._storage(before.owner)
        warnings = await self._capture(before, "ha_config_set_dashboard")
        current, metadata_updated = await self._metadata_change(
            before, candidate, metadata, action, warnings
        )
        if candidate is not None:
            current = await self.storage.save(current, candidate)
            warnings.extend(current.warnings)
        if action != "set":
            result = edit_dashboard_result(
                current.url_path,
                {
                    "config": current.config,
                    "config_hash": compute_config_hash(current.config)
                    if current.verified and current.config is not None
                    else None,
                    "write_committed": True,
                    "post_write_verified": current.verified,
                    "warnings": warnings,
                },
                action,
                resolved,
                python_transform=python_transform,
            )
        else:
            if candidate is not None and (
                warning := _large_dashboard_replacement_warning(
                    len(json.dumps(before.config))
                )
            ):
                warnings.append(warning)
            result = set_dashboard_result(
                current.url_path,
                dashboard_exists=before.owner is not None,
                dashboard_id=current.row.get("id") if current.row else None,
                config_updated=candidate is not None,
                metadata_updated=metadata_updated,
                warnings=warnings,
                pre_resolved_from=resolved,
            )
            result.update(
                config_hash=compute_config_hash(current.config)
                if current.verified and current.config is not None
                else None,
                write_committed=True,
                post_write_verified=current.verified,
            )
            if current.verified:
                _attach_dashboard_render_paths(result, current.url_path, current.config)
        return result, current

    async def _metadata_change(
        self,
        before: DashboardState,
        candidate: Any,
        metadata: dict[str, Any],
        action: str,
        warnings: list[str],
    ) -> tuple[DashboardState, bool]:
        if before.owner is None:
            create = {
                **metadata,
                "title": metadata.get("title")
                or before.url_path.replace("-", " ").title(),
            }
            if create.get("icon") is None:
                create.pop("icon", None)
            return await self.storage.create(before, create), False
        if metadata and action == "set":
            if before.row is None and candidate is not None:
                warnings.append(
                    "Metadata fields were provided but could not be applied: dashboard has no storage ID (likely the built-in default dashboard). Dashboard config was saved."
                )
            else:
                return await self.storage.update_metadata(before, metadata), True
        return before, False

    async def delete(self, url_path: str) -> dict[str, Any]:
        try:
            validate_identifier_not_empty(url_path, "url_path")
            await admit_registry_write(self.actor, self.manager.backend.authorize)
            canonical, _owner = self.storage.resolve(url_path)
            async with registry_update_lock("dashboard", canonical):
                before = await self.storage.read(canonical)
                if before.row is None:
                    raise_dashboard_edit_error(
                        canonical,
                        "not_found",
                        "No storage dashboard can be deleted at this path",
                        False,
                        "delete",
                    )
                warnings = await self._capture(before, "ha_config_delete_dashboard")
                await self.storage.remove(before)
                await authorize_actor(self.actor, self.manager.backend.authorize)
                result: dict[str, Any] = {
                    "success": True,
                    "action": "delete",
                    "url_path": "lovelace" if url_path == "default" else url_path,
                    "message": "Dashboard deleted successfully",
                }
                if before.row["id"] != result["url_path"]:
                    result["resolved_id"] = before.row["id"]
                if canonical == "lovelace":
                    result["note"] = (
                        "Home Assistant re-registers its auto-generated default dashboard at /lovelace."
                    )
                if warnings:
                    result["warnings"] = warnings
                return result
        except Exception as error:
            raise self.storage.failure(error) from error
