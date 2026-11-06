"""
Backup and restore tools for Home Assistant MCP Server.

Provides the polymorphic ``ha_manage_backup`` tool, which handles both:

* **Full HA snapshots** (``scope="snapshot"``) — the original
  ``ha_backup_create`` / ``ha_backup_restore`` functionality. Heavy
  tarball creation via HA's native backup integration; restore restarts
  HA. Last-resort recovery. Implemented in ``backup_snapshot.py`` and
  ``backup_snapshot_restore.py``.
* **Per-edit auto-backups** (``scope="edits"``) — list / view / restore
  / delete operations against the per-entity snapshots produced by the
  ``@with_auto_backup`` decorator (#1288). Lightweight, no HA restart.

The merge consolidates the previous two tools so the LLM cannot
accidentally route "restore my automation" through the heavyweight full
HA restore path. Each call must explicitly pick its scope.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.backends.legacy import legacy_errors
from ha_mcp.runtime.backup_hints import BACKUP_HINTS
from ha_mcp.runtime.backup_tools import BackupFacade, backup_description

from ..backup_manager import (
    LEGACY_PREFIX,
    BackupManager,
    BackupRestoreError,
    MandatoryBackupError,
    SnapshotInUseError,
    get_backup_manager,
    restore_error_code,
)
from ..backup_todo import TodoListNotLoaded
from ..client.rest_client import HomeAssistantClient
from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .auto_backup import raise_if_snapshot_path_refused
from .backup_access import (
    gate_backup_combo as _gate_combo,
)
from .backup_access import (
    require_backup_access,
)
from .backup_access import (
    require_backup_param as _require,
)
from .backup_on_demand import capture_on_demand
from .backup_snapshot import create_backup, delete_backup, list_backups
from .backup_snapshot_restore import restore_backup
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
)
from .tool_hints import write_hints

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _get_backup_hint_text() -> str:
    """Return the fixed guidance after upstream removed BACKUP_HINT."""
    return BACKUP_HINTS["normal"]


class BackupTools(BackupFacade):
    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client
        super().__init__(self._manage)

    ha_manage_backup = tool(
        tags={"System"},
        annotations=write_hints(
            "Manage Backups", destructive=True, idempotent=False, open_world=False
        ),
    )(log_tool_usage(legacy_errors(BackupFacade.ha_manage_backup)))

    async def _manage(self, **arguments: Any) -> dict[str, Any]:
        scope, action = arguments.pop("scope"), arguments.pop("action")
        _gate_combo(scope, action)
        settings = get_global_settings()
        require_backup_access(settings, scope, action)
        if scope == "snapshot":
            return await _dispatch_snapshot_action(
                self._client,
                action,
                scope=scope,
                **{
                    key: arguments[key]
                    for key in (
                        "name",
                        "backup_id",
                        "restore_database",
                        "restore_addons",
                        "restore_folders",
                        "safety_backup",
                        "parts",
                        "confirm",
                        "limit",
                        "ctx",
                    )
                },
            )
        return await _dispatch_edits_action(
            get_backup_manager(self._client, settings),
            settings,
            action,
            scope=scope,
            **{
                key: arguments[key]
                for key in (
                    "domain",
                    "entity_id",
                    "backup_name",
                    "older_than_days",
                    "limit",
                )
            },
        )


def register_backup_tools(
    mcp: "FastMCP", client: HomeAssistantClient, **kwargs: Any
) -> None:
    registered = mcp.add_tool(BackupTools(client).ha_manage_backup)
    registered.description = backup_description(_get_backup_hint_text())


async def _dispatch_snapshot_action(
    client: HomeAssistantClient,
    action: str,
    *,
    scope: str,
    name: str | None,
    backup_id: str | None,
    restore_database: bool | None,
    restore_addons: list[str] | None,
    restore_folders: list[str] | None,
    safety_backup: bool,
    parts: dict[str, Any],
    confirm: bool,
    limit: int,
    ctx: Context | None,
) -> dict[str, Any]:
    """Route a ``scope="snapshot"`` action to its full-tarball handler.

    ``parts`` holds the ``include_*`` parameters of a custom backup.
    """
    if action == "create":
        return await create_backup(client, name, ctx=ctx, **parts)
    if action == "list":
        return await list_backups(client, limit)
    if action == "delete":
        bid = _require("backup_id", backup_id, scope, action)
        return await delete_backup(client, bid, confirm, ctx=ctx)
    # action == "restore"
    bid = _require("backup_id", backup_id, scope, action)
    return await restore_backup(
        client,
        bid,
        restore_database,
        ctx=ctx,
        restore_addons=restore_addons,
        restore_folders=restore_folders,
        safety_backup=safety_backup,
    )


async def _dispatch_edits_action(
    mgr: BackupManager,
    settings: Any,
    action: str,
    *,
    scope: str,
    domain: str | None,
    entity_id: str | None,
    backup_name: str | None,
    older_than_days: int | None,
    limit: int,
) -> dict[str, Any]:
    """Route a ``scope="edits"`` action to its per-entity auto-backup handler."""
    if action == "create":
        return await _edits_create(mgr, scope, action, domain, entity_id)
    if action == "list":
        return await _edits_list(mgr, settings, domain, entity_id, limit)
    if action == "view":
        return await _edits_view(mgr, scope, action, backup_name)
    if action == "diff":
        return await _edits_diff(mgr, scope, action, backup_name)
    if action == "restore":
        return await _edits_restore(mgr, scope, action, backup_name)
    # action == "delete"
    return await _edits_delete(mgr, domain, entity_id, backup_name, older_than_days)


async def _edits_create(
    mgr: BackupManager,
    scope: str,
    action: str,
    domain: str | None,
    entity_id: str | None,
) -> dict[str, Any]:
    """(edits, create) On-demand force-snapshot of one entity."""
    # On-demand snapshot for "I'm about to edit this in the HA UI,
    # save the current state first." Drives the same handler path
    # the ``@with_auto_backup`` decorator uses on writes, but goes
    # through ``mgr.maybe_snapshot(force=True)`` which bypasses
    # both the ``enable_auto_backup`` toggle and the per-entity
    # throttle window — the request is explicit, so neither
    # should suppress it.
    dom = _require("domain", domain, scope, action)
    eid = _require("entity_id", entity_id, scope, action)
    if mgr.handler_for(dom) is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"No backup handler registered for domain={dom!r}",
                context={"domain": dom, "entity_id": eid},
                suggestions=[
                    "Supported domains: " + ", ".join(mgr.supported_domains()),
                ],
            )
        )
    path = await capture_on_demand(mgr, dom, eid)
    # The id the snapshot is stored under: a flow helper's config entry id
    # when the caller passed an entity_id alias, else the id as given.
    eid = await asyncio.to_thread(mgr._payload_entity_id, path) or eid
    return {
        "success": True,
        "data": {
            "backup_name": path.name,
            "domain": dom,
            "entity_id": eid,
            "size": path.stat().st_size,
        },
    }


async def _edits_list(
    mgr: BackupManager,
    settings: Any,
    domain: str | None,
    entity_id: str | None,
    limit: int,
) -> dict[str, Any]:
    """(edits, list) List per-entity auto-backups plus legacy .bak entries."""
    # Edits-store snapshots + pre-#1579 legacy .bak entries (#1579).
    # The manager owns the merge (sync dir-glob off-thread + async
    # legacy service call) so this layer stays source-agnostic.
    entries, warnings = await mgr.list_edits_and_legacy(
        domain=domain,
        entity_id=entity_id,
        limit=limit,
    )
    response: dict[str, Any] = {
        "success": True,
        "data": {
            "backups": entries,
            "count": len(entries),
            "backup_dir": str(mgr.backup_dir),
            "enabled": mgr.enabled,
            "throttle_minutes": settings.auto_backup_throttle_minutes,
            "retain_per_entity": settings.auto_backup_retain_per_entity,
        },
    }
    if warnings:
        response["warnings"] = warnings
    return response


async def _edits_view(
    mgr: BackupManager,
    scope: str,
    action: str,
    backup_name: str | None,
) -> dict[str, Any]:
    """(edits, view) Read one auto-backup file by name."""
    bname = _require("backup_name", backup_name, scope, action)
    try:
        if bname.startswith(LEGACY_PREFIX):
            # Legacy read is an async service call (component-side store),
            # not a local-dir read; same FileNotFound/ValueError contract.
            data = await mgr.read_legacy(bname[len(LEGACY_PREFIX) :])
        else:
            data = await asyncio.to_thread(mgr.read_snapshot, bname)
    except FileNotFoundError:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Backup {bname!r} not found",
                context={"backup_name": bname},
            )
        )
    except ValueError as err:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                str(err),
                context={"backup_name": bname},
            )
        )
    return {"success": True, "data": data}


async def _edits_diff(
    mgr: BackupManager,
    scope: str,
    action: str,
    backup_name: str | None,
) -> dict[str, Any]:
    """(edits, diff) Compare one auto-backup against the entity's live config."""
    bname = _require("backup_name", backup_name, scope, action)
    try:
        diff = await mgr.diff_snapshot(bname)
    except FileNotFoundError:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Backup {bname!r} not found",
                context={"backup_name": bname},
            )
        )
    except (ValueError, LookupError) as err:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                str(err),
                context={"backup_name": bname},
            )
        )
    except TodoListNotLoaded as err:
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_UNAVAILABLE,
                str(err),
                context={"backup_name": bname},
            )
        )
    except ToolError as err:
        raise_if_snapshot_path_refused(err, "diff", bname)
        raise
    except Exception as err:  # noqa: BLE001
        # Fetching the live config for diff goes through the
        # same domain handler ``restore`` uses, so the same
        # HA-side failure modes (4xx/5xx, WS errors, schema
        # drift) apply. Funnel through
        # ``exception_to_structured_error`` so the structured
        # response carries enough context to retry.
        exception_to_structured_error(
            err,
            context={"backup_name": bname, "action": "diff"},
            suggestions=[
                "Verify the entity referenced by the backup still "
                + "exists; diff fetches its current config",
                "Inspect the snapshot YAML via "
                + "ha_manage_backup(scope='edits', action='view', "
                + "backup_name=...) to confirm it parses",
            ],
        )
        return None  # unreachable: exception_to_structured_error always raises
    warnings: list[str] = []
    if diff.get("entity_missing"):
        # ``restore_snapshot`` outcome on a missing entity is
        # domain-dependent: upsert paths (automation, script,
        # dashboard) recreate it, but helper / label / category
        # restores go through ``<domain>/update`` WS commands
        # that expect the entity to exist and would surface a
        # WS error if it does not. Hedge rather than promise
        # one specific outcome.
        warnings.append(
            "Entity is missing from HA; restore behaviour is "
            "domain-dependent (upsert paths recreate it; "
            "update-only paths return an error)"
        )
    if diff.get("truncated"):
        # Text snapshots (file/YAML) carry a unified diff, not a
        # JSON-Patch — name it accordingly.
        noun = "Diff" if diff.get("kind") == "text" else "Patch"
        warnings.append(
            f"{noun} truncated; the target has more changes than the "
            "bounded diff captures — view the snapshot for the full state"
        )
    return {
        "success": True,
        "data": diff,
        **({"warnings": warnings} if warnings else {}),
    }


async def _edits_restore(
    mgr: BackupManager,
    scope: str,
    action: str,
    backup_name: str | None,
) -> dict[str, Any]:
    """Re-apply an edit backup, or recreate a deleted flow helper or subentry."""
    bname = _require("backup_name", backup_name, scope, action)
    try:
        result = await mgr.restore_snapshot(bname)
    except FileNotFoundError:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Backup {bname!r} not found",
                context={"backup_name": bname},
            )
        )
    except (ValueError, LookupError) as err:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                str(err),
                context={"backup_name": bname},
            )
        )
    except BackupRestoreError as err:
        code = restore_error_code(err)
        hint = "Inspect the current configuration and restore outcome before retrying"
        suggestions = list(err.outcome.get("suggestions") or [hint])
        if err.outcome.get("safety_backup"):
            suggestions.append(
                "Use safety_backup to inspect or restore the captured previous state"
            )
        raise_tool_error(
            create_error_response(
                code,
                str(err),
                context={"backup_name": bname, "data": err.outcome},
                suggestions=suggestions,
            )
        )
    except MandatoryBackupError as err:
        # A restore's mandatory pre-restore safety snapshot genuinely
        # failed — the overwrite was blocked, nothing changed. Same
        # fail-closed contract + error code as the @with_auto_backup write
        # path (#1579), so callers see one consistent failure mode.
        not_applied = {
            "restored_from": bname,
            "safety_backup": None,
            "apply_status": "not_applied",
            "verification_status": "not_run",
        }
        # Its live file's path is no longer allowed: retrying can't help.
        raise_if_snapshot_path_refused(err, "restore", bname, not_applied)
        logger.warning("Restore of %s blocked: %s", bname, err.safe_cause)
        raise_tool_error(
            create_error_response(
                ErrorCode.BACKUP_CAPTURE_FAILED,
                "Restore blocked: the pre-restore safety snapshot could not be "
                f"captured ({err.safe_cause}). Nothing was changed.",
                context={"backup_name": bname, "data": not_applied},
                suggestions=err.suggestions
                or ["Retry once the underlying issue is resolved"],
            )
        )
    except ToolError as err:
        outcome = getattr(err, "restore_outcome", None)
        raise_if_snapshot_path_refused(err, "restore", bname, outcome)
        raise
    except Exception as err:  # noqa: BLE001
        # ``handler.restore`` is domain-specific and can surface HA-side
        # rejections (schema-validation failures, 4xx/5xx responses, WS command
        # errors). Without this catch those propagate as opaque INTERNAL_ERROR
        # with no ``backup_name`` / ``domain`` context. Funnel through
        # ``exception_to_structured_error``, with the manager's outcome (the
        # safety backup to revert with, warnings) as ``data``.
        exception_to_structured_error(
            err,
            context={
                "backup_name": bname,
                "action": "restore",
                "data": getattr(err, "restore_outcome", None),
            },
            suggestions=[
                "Verify the entity referenced by the backup still exists; "
                + "restore re-POSTs to its current registry key",
                "Compare the captured schema vs current HA — HA minor versions "
                + "occasionally drop/rename fields",
                "Inspect the snapshot YAML via ha_manage_backup(scope='edits', "
                + "action='view', backup_name=...)",
            ],
        )
        return None  # unreachable: exception_to_structured_error always raises
    # The manager also serves Settings, which consumes handler warnings in place.
    # Copy its result before moving warnings to the MCP response envelope.
    data = dict(result)
    warnings: list[str] = list(data.pop("warnings", []))
    if isinstance(result.get("result"), dict):
        handler_result = dict(result["result"])
        warnings.extend(handler_result.pop("warnings", []))
        data["result"] = handler_result
    if result.get("safety_backup"):
        warnings.append(
            "This restore did NOT restart HA. To revert, restore the safety_backup."
        )
    return {
        "success": True,
        "data": data,
        **({"warnings": list(dict.fromkeys(warnings))} if warnings else {}),
    }


async def _edits_delete(
    mgr: BackupManager,
    domain: str | None,
    entity_id: str | None,
    backup_name: str | None,
    older_than_days: int | None,
) -> dict[str, Any]:
    """(edits, delete) Delete one auto-backup by name, or bulk-delete by filter."""
    if backup_name:
        try:
            await asyncio.to_thread(mgr.delete_snapshot, backup_name)
        except SnapshotInUseError as err:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(err),
                    context={
                        "backup_name": backup_name,
                        "data": {"reason": "snapshot_in_use"},
                    },
                    suggestions=[
                        "Retry deletion after the active capture or restore finishes"
                    ],
                )
            )
        except FileNotFoundError:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Backup {backup_name!r} not found",
                    context={"backup_name": backup_name},
                )
            )
        except ValueError as err:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(err),
                    context={"backup_name": backup_name},
                )
            )
        return {"success": True, "data": {"deleted": [backup_name]}}
    # Bulk
    if domain is None and entity_id is None and older_than_days is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "edits.delete requires backup_name OR at least one filter "
                "(domain, entity_id, older_than_days)",
                suggestions=[
                    "Pass backup_name to delete one auto-backup",
                    "Pass domain/entity_id/older_than_days to bulk-delete (requires at least one)",
                ],
            )
        )
    bulk = await asyncio.to_thread(
        mgr.delete_bulk,
        domain=domain,
        entity_id=entity_id,
        older_than_days=older_than_days,
    )
    deleted = bulk["deleted"]
    failed = bulk["failed"]
    warnings = (
        [f"Failed to delete {len(failed)} backup(s); see server log"] if failed else []
    )
    in_use_count = sum(
        reason == "snapshot_in_use"
        for reason in bulk.get("failure_reasons", {}).values()
    )
    if in_use_count:
        warnings.append(
            f"{in_use_count} backup(s) are in use by a capture or restore; "
            "retry deletion after it finishes."
        )
    return {
        "success": True,
        "data": {
            **bulk,
            "count": len(deleted),
            "failed_count": len(failed),
        },
        **({"warnings": warnings} if warnings else {}),
    }
