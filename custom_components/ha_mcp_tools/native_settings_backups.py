"""Administrator snapshot operations use the same engine and store as tools."""

from __future__ import annotations

from typing import Any

from aiohttp import web

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.snapshot_files import SnapshotInUseError
from ha_mcp.runtime.snapshot_payloads import (
    BackupRestoreError,
    _FlowHelperReadError,
    _snapshot_validation_message,
)
from ha_mcp.runtime.snapshot_projection import panel_diff, restore_error
from ha_mcp.tool_api import CallContext, ToolFailure, ToolResult

from .native_settings_context import settings_context


class NativeBackupSettings:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def handle(self, request: web.Request, suffix: str) -> web.Response:
        manager = self.settings.manager
        try:
            await manager.refresh_policy()
            actor = await settings_context(manager.hass, request)
            arguments = self._arguments(request, suffix)
            if arguments is None:
                return web.Response(status=405)
            writes = arguments["action"] in {"restore", "delete"}

            async def invoke() -> ToolResult:
                try:
                    if writes:
                        manager.snapshots._require_restore_access(actor)
                    data = await self._invoke(arguments, actor)
                    return ToolResult(data)
                except BackupRestoreError as error:
                    payload, _status = restore_error(error)
                    status = {
                        "not_applied": "not_started",
                        "applied": "confirmed",
                        "unknown": "outcome_unknown",
                    }[error.outcome["apply_status"]]
                    return ToolResult(payload, is_error=True, status=status)
                except (
                    FileNotFoundError,
                    SnapshotInUseError,
                    ValueError,
                    _FlowHelperReadError,
                ) as error:
                    return self._known_error(error, arguments.get("name", ""))

            result = await manager.engine.call_owned(
                "ha_manage_backup",
                invoke,
                actor,
                writes=writes,
                arguments=arguments,
                enforce_catalogue=False,
            )
        except PermissionError:
            return web.json_response(
                {"error": "Administrator session required"}, status=403
            )
        except ToolFailure as error:
            result = ToolResult(
                error.data, is_error=True, status=error.status or "not_started"
            )
        except ValueError as error:
            return web.json_response(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER, str(error)
                ),
                status=400,
            )
        return web.json_response(result.data, status=self._http_status(result))

    @staticmethod
    def _arguments(request: web.Request, suffix: str) -> dict[str, Any] | None:
        name, _, operation = suffix.partition("/")
        action = {
            ("GET", False, ""): "list",
            ("DELETE", False, ""): "delete",
            ("GET", True, ""): "view",
            ("DELETE", True, ""): "delete",
            ("GET", True, "diff"): "diff",
            ("POST", True, "restore"): "restore",
        }.get((request.method, bool(name), operation))
        if action is None:
            return None
        args: dict[str, Any] = {"scope": "edits", "action": action}
        if name:
            args["name"] = name
        else:
            for key in ("domain", "entity_id"):
                if request.query.get(key):
                    args[key] = request.query[key]
            if action == "list":
                args["limit"] = max(
                    1, min(10_000, int(request.query.get("limit", "500")))
                )
            elif request.query.get("older_than_days") is not None:
                args["older_than_days"] = int(request.query["older_than_days"])
            if action == "delete" and not any(
                key in args for key in ("domain", "entity_id", "older_than_days")
            ):
                raise ValueError(
                    "Bulk delete requires at least one filter (domain, entity_id, older_than_days)"
                )
        return args

    async def _invoke(self, args: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        manager = self.settings.manager
        snapshots = manager.snapshots
        action, name = args["action"], args.get("name", "")
        if action == "restore":
            return {
                "success": True,
                "data": await snapshots.restore_snapshot(name, actor),
            }
        if action == "diff":
            data, current = await snapshots.snapshot_comparison(name, actor)
            result: dict[str, Any] = await snapshots._io(
                panel_diff, name, data, current
            )
            return result
        store = snapshots._store()
        if action == "view":
            return {
                "success": True,
                "data": await snapshots._io(store.read_snapshot, name),
            }
        if action == "list":
            rows = await snapshots._io(
                lambda: store.list_snapshots(
                    domain=args.get("domain"),
                    entity_id=args.get("entity_id"),
                    limit=args["limit"],
                )
            )
            return {
                "success": True,
                "backups": rows,
                "count": len(rows),
                "backup_dir": str(store.directory),
                "enabled": manager.policy.flag("enable_auto_backup"),
                "throttle_minutes": store.throttle_seconds / 60,
                "retain_per_entity": store.retention,
            }
        await manager.backend.authorize(actor)
        snapshots._require_restore_access(actor)
        if name:
            await snapshots._io(store.delete_snapshot, name)
            return {"success": True, "deleted": [name]}
        bulk = await snapshots._io(
            lambda: store.delete_bulk(
                domain=args.get("domain"),
                entity_id=args.get("entity_id"),
                older_than_days=args.get("older_than_days"),
            )
        )
        return {
            "success": True,
            **bulk,
            "count": len(bulk["deleted"]),
            "failed_count": len(bulk["failed"]),
        }

    @staticmethod
    def _known_error(error: Exception, name: str) -> ToolResult:
        if isinstance(error, FileNotFoundError):
            payload = create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND, f"Backup {name!r} not found"
            )
        elif isinstance(error, SnapshotInUseError):
            payload = create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                str(error),
                context={"data": {"reason": "snapshot_in_use"}},
            )
        elif isinstance(error, _FlowHelperReadError):
            payload = create_error_response(
                ErrorCode.CONFIG_VALIDATION_FAILED,
                str(error),
                context={"data": {"reason": error.reason}},
            )
        else:
            assert isinstance(error, ValueError)
            payload = create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                _snapshot_validation_message(error),
            )
        return ToolResult(payload, is_error=True, status="not_started")

    @staticmethod
    def _http_status(result: ToolResult) -> int:
        if not result.is_error:
            return 200
        code = (
            result.data.get("error", {}).get("code")
            if isinstance(result.data, dict)
            else None
        )
        return {
            "RESOURCE_NOT_FOUND": 404,
            "VALIDATION_INVALID_PARAMETER": 400,
            "UNSUPPORTED_OPERATION": 501,
            "AUTH_REQUIRED": 403,
            "READ_ONLY": 403,
        }.get(str(code), 409)
