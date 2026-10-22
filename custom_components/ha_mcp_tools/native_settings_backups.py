"""Snapshot UI file operations over the same store used by native tool writes."""

from __future__ import annotations

from typing import Any

from aiohttp import web

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.snapshot_files import SnapshotInUseError


class NativeBackupSettings:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def handle(self, request: web.Request, suffix: str) -> web.Response:
        await self.settings.manager.refresh_policy()
        store = self.settings.manager.snapshots._store()
        try:
            if not suffix:
                return await self._collection(request, store)
            if "/" in suffix:
                return web.json_response(
                    {
                        "error": "Live comparison and restore for this native snapshot domain are not available yet; no configuration was changed"
                    },
                    status=501,
                )
            if request.method == "GET":
                data = await self.settings._io(store.read_snapshot, suffix)
                return web.json_response({"success": True, "data": data})
            if request.method == "DELETE":
                await self.settings._io(store.delete_snapshot, suffix)
                return web.json_response({"success": True, "deleted": [suffix]})
            return web.Response(status=405)
        except FileNotFoundError:
            return web.json_response(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND, f"Backup {suffix!r} not found"
                ),
                status=404,
            )
        except SnapshotInUseError as error:
            return web.json_response(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(error),
                    context={"data": {"reason": "snapshot_in_use"}},
                ),
                status=409,
            )
        except ValueError as error:
            from ha_mcp.runtime.snapshot_payloads import _snapshot_validation_message

            return web.json_response(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    _snapshot_validation_message(error),
                ),
                status=400,
            )

    async def _collection(self, request: web.Request, store: Any) -> web.Response:
        if request.method == "DELETE":
            return await self._delete_many(request, store)
        if request.method != "GET":
            return web.Response(status=405)
        limit = max(1, min(10_000, int(request.query.get("limit", "500"))))
        rows = await self.settings._io(
            lambda: store.list_snapshots(
                domain=request.query.get("domain") or None,
                entity_id=request.query.get("entity_id") or None,
                limit=limit,
            )
        )
        return web.json_response(
            {
                "success": True,
                "backups": rows,
                "count": len(rows),
                "backup_dir": str(store.directory),
                "enabled": self.settings.manager.policy.flag("enable_auto_backup"),
                "throttle_minutes": store.throttle_seconds / 60,
                "retain_per_entity": store.retention,
            }
        )

    async def _delete_many(self, request: web.Request, store: Any) -> web.Response:
        domain = request.query.get("domain") or None
        entity_id = request.query.get("entity_id") or None
        older = request.query.get("older_than_days")
        age = int(older) if older is not None else None
        if domain is None and entity_id is None and age is None:
            raise ValueError(
                "Bulk delete requires at least one filter (domain, entity_id, older_than_days)"
            )
        result = await self.settings._io(
            lambda: store.delete_bulk(
                domain=domain, entity_id=entity_id, older_than_days=age
            )
        )
        return web.json_response(
            {
                "success": True,
                **result,
                "count": len(result["deleted"]),
                "failed_count": len(result["failed"]),
            }
        )
