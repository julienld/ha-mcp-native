"""Administrator filesystem settings use the same component-owned permission store."""

from typing import Any

from aiohttp import web

from ha_mcp.tool_api import ToolFailure

from .const import (
    ALLOWED_READ_DIRS,
    ALLOWED_WRITE_DIRS,
    DENY_PATH_SEGMENTS,
    DENY_READ_BASENAMES,
)
from .file_access import _current_extra_dirs
from .native_operations.file_entry import NativeFileEntry
from .native_settings_context import settings_context


class NativeFileSettings:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def handle(self, request: web.Request) -> web.Response:
        manager = self.settings.manager
        try:
            await manager.refresh_policy()
            actor = await settings_context(manager.hass, request)
            entry = NativeFileEntry(manager, actor, require_auto_backup=False)
            owner = await entry._owner()
            if request.method == "GET":
                return web.json_response(
                    {
                        "success": True,
                        "available": True,
                        "paths": list(_current_extra_dirs(manager.hass)),
                        "deny_floor": sorted(DENY_PATH_SEGMENTS | DENY_READ_BASENAMES),
                        "builtin_read_dirs": list(ALLOWED_READ_DIRS),
                        "builtin_write_dirs": list(ALLOWED_WRITE_DIRS),
                    }
                )
            if request.method != "POST":
                return web.Response(status=405)
            body = await request.json()
            paths = body.get("paths", []) if isinstance(body, dict) else None
            if not isinstance(paths, list) or not all(
                isinstance(item, str) for item in paths
            ):
                raise ValueError("'paths' must be a list of directory strings")

            async def admin_guard(_writes: bool) -> None:
                # Like the feature/readonly toggles, this is an administrator
                # setting, not an MCP tool call governed by its catalogue state.
                await entry.guard(False)

            async def save(*, guard: Any, state: Any) -> dict[str, Any]:
                from . import _apply_allowed_paths

                async def before_store() -> None:
                    await admin_guard(False)
                    state.started = True

                kept, rejected = await _apply_allowed_paths(
                    manager.hass, paths, guard=before_store
                )
                state.committed = True
                return {
                    "success": True,
                    "applied": kept,
                    "paths": kept,
                    "rejected": rejected,
                    "mode": "component",
                    "restart_required": False,
                }

            return web.json_response(
                await owner.run_owned(save, admin_guard, writes=True)
            )
        except PermissionError:
            return web.json_response(
                {"error": "Administrator session required"}, status=403
            )
        except ToolFailure as error:
            if request.method == "GET" and error.code in {
                "FEATURE_DISABLED",
                "COMPONENT_NOT_INSTALLED",
            }:
                return web.json_response(
                    {
                        "success": True,
                        "available": False,
                        "reason": str(error),
                        "paths": [],
                        "deny_floor": [],
                    }
                )
            status = 403 if error.code == "AUTH_FORBIDDEN" else 409
            return web.json_response(error.data, status=status)
