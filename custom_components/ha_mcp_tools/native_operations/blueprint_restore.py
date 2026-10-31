"""Restore captured YAML through Core, retaining the observed domain and file."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ha_mcp.runtime.blueprint_values import normalize_blueprint_path
from ha_mcp.runtime.snapshot_payloads import InvalidBackupSnapshotError
from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.registry_update_lock import registry_update_lock

from ..blueprint_files import InstalledBlueprint
from .blueprint_management import NativeBlueprints
from .blueprint_owner import BlueprintOwner


@dataclass(slots=True)
class NativeBlueprintRestore:
    store: BlueprintOwner
    path: str
    saved: str = field(repr=False)
    state: InstalledBlueprint | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.saved, str):
            raise InvalidBackupSnapshotError(
                "A blueprint snapshot must contain YAML text"
            )
        self.path = normalize_blueprint_path(
            NativeBlueprints.validated(self.path, "path")
        )

    async def current(self) -> str | None:
        self.state = await self.store.read(self.path)
        return self.state.text

    comparison = current

    async def apply(self, before: str | None) -> dict[str, Any]:
        if self.state is None or self.state.text != before:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Blueprint restore no longer holds its safety before-image",
                status="not_started",
            )
        try:
            async with registry_update_lock(
                f"blueprint_{self.store.domain}", self.path
            ):
                blueprint = await NativeBlueprints.prepare(self.store, self.saved, None)
                replaced = await self.store.write(
                    self.path, self.state, blueprint, overwrite=True
                )
            return {
                "domain": self.store.domain,
                "path": self.path,
                "overrides_existing": replaced,
                "post_write_verified": True,
            }
        except Exception as error:
            raise self.store.failure(error) from error
