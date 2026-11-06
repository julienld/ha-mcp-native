"""Capture one authoritative registry preimage before a bounded mutation."""

import copy
from typing import Any

from ha_mcp.tool_api import BackendTools, UnsupportedOperation


class RegistryWriteContext(BackendTools):
    async def _registry_change(
        self, domain: str, action: str, data: dict[str, Any], source_tool: str
    ) -> dict[str, Any]:
        if domain not in {"category", "area", "floor"} or action not in {
            "create",
            "update",
            "delete",
        }:
            raise UnsupportedOperation(f"registry.{domain}.{action}")
        identifier = data.get(f"{domain}_id")
        target = {f"{domain}_id": identifier}
        if domain == "category":
            target["scope"] = data["scope"]
        previous = (
            await self.backend.execute(f"registry.{domain}.read", target, self.context)
            if identifier is not None
            else None
        )
        snapshot = copy.deepcopy(previous)
        snapshot_domain, backup_id = domain, identifier
        if domain == "category":
            backup_id = (
                f"{data['scope']}:{identifier}" if identifier is not None else None
            )
            if snapshot is not None:
                snapshot["scope"] = data["scope"]
        elif domain in {"area", "floor"}:
            snapshot_domain = "area_or_floor"
            backup_id = f"{domain}:{identifier}" if identifier is not None else None
            if snapshot is not None:
                snapshot["kind"] = domain
        backup = await self.backend.execute(
            "registry.backup.capture",
            {
                "domain": snapshot_domain,
                "source_tool": source_tool,
                "backup_id": backup_id,
                "current_config": snapshot,
            },
            self.context,
        )
        result: dict[str, Any] = await self.backend.execute(
            f"registry.{domain}.{action}",
            {"data": data, "expected_item": previous},
            self.context,
        )
        if backup and backup.get("warnings"):
            result.setdefault("warnings", []).extend(backup["warnings"])
        return result
