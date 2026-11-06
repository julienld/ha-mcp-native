"""Native manager diagnostics, deferred reload and its actual approval queue."""

import sys
from typing import Any

from ha_mcp import __version__
from ha_mcp.tool_api import ToolFailure
from ha_mcp.tools.server_version import running_version_report

from ..const import COMPONENT_VERSION
from .developer_settings import NativeDeveloperSettings


class NativeDeveloperServer(NativeDeveloperSettings):
    def _info(self) -> dict[str, Any]:
        from homeassistant.const import __version__ as core_version

        versions, warnings = running_version_report()
        if versions["version_mismatch"]:
            warnings = [
                "The installed runtime differs from the running version; restart Home Assistant to load changed Python packages."
            ]
        response: dict[str, Any] = {
            "success": True,
            "data": {
                **versions,
                "version": __version__,
                "python_version": sys.version.split()[0],
                "data_dir": str(self.ui.directory),
                "ha_version": core_version,
                "component_server_entry": {
                    "entry_id": self.manager.entry.entry_id,
                    "pip_spec": None,
                    "role": "this server (native HACS runtime bundle)",
                },
                "component_version": COMPONENT_VERSION,
                "deployment_mode": "embedded",
                "runtime": "native",
                "is_addon": False,
                "is_embedded": True,
                "entry_id": self.manager.entry.entry_id,
                "restart_supported": True,
                "update_source_supported": False,
                "note": "Update the integration through HACS. Reload restarts this entry; restart Home Assistant to load changed Python packages.",
            },
        }
        if warnings:
            response["warnings"] = warnings
        return response

    async def manage_server(
        self, action: str, pip_spec: str | None = None, token: str | None = None
    ) -> dict[str, Any]:
        await self.guard()
        if action == "info":
            return await self._call(self._info)
        if action == "update_source":
            raise ToolFailure(
                "UNSUPPORTED_OPERATION",
                "Native HACS uses the component's installed runtime bundle. Update the integration through HACS; pip_spec changes are unavailable.",
                status="not_started",
            )
        if action == "restart":
            await self.guard(True)
            self.manager.system_lifecycle.schedule(
                "entry_reload", self.actor, entry=self.manager.entry
            )
            return {
                "success": True,
                "data": {
                    "scheduled": True,
                    "mode": "embedded",
                    "note": "Config entry reload scheduled; this connection will drop. Reconnect after the entry is ready.",
                    "deployment_mode": "embedded",
                    "restart_scheduled": True,
                    "message": "Integration reload scheduled; completion is not yet observed",
                },
            }
        queue = (
            self.manager.approval_gate.queue
            if self.manager.approval_gate is not None
            else None
        )
        if action == "list_pending":
            await self.guard()
            pending = (
                [
                    {
                        "token": item.token,
                        "tool_name": item.tool_name,
                        "args": item.args,
                        "created_at": item.created_at.isoformat(),
                        "expires_at": item.expires_at.isoformat(),
                    }
                    for item in queue.list_pending()
                ]
                if queue is not None
                else []
            )
            return {
                "success": True,
                "data": {"pending": pending, "count": len(pending)},
            }
        if action not in {"approve", "deny"}:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Unknown developer action",
                status="not_started",
            )
        await self.guard(True, security=True)
        if queue is None or not token or (item := queue.get(token)) is None:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Unknown or expired approval token",
                status="not_started",
            )
        outcome = queue.decide_with_outcome(token, action)
        if outcome.value != "applied":
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Token already decided",
                status="not_started",
            )
        return {
            "success": True,
            "data": {
                "token": token,
                "tool_name": item.tool_name,
                "decision": "approved" if action == "approve" else "denied",
            },
        }
