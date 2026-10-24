"""Restore group configuration while retaining the Core owner read for safety."""

from dataclasses import dataclass, field
from typing import Any

from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from . import group_records as records
from .groups import NativeGroups
from .registry_access import authorize_actor


def group_identity(reference: str, config: dict[str, Any]) -> str:
    identifier = reference.removeprefix("group.")
    if (
        not identifier
        or "." in identifier
        or config.get("object_id", identifier) != identifier
    ):
        raise ToolFailure(
            "INVALID_SNAPSHOT",
            "Group snapshot does not match its identity",
            status="not_started",
        )
    return identifier


@dataclass(slots=True)
class NativeGroupRestore:
    service: NativeGroups
    reference: str
    config: dict[str, Any]
    component: Any = field(default=None, init=False)
    owner: Any = field(default=None, init=False)
    full_before: dict[str, Any] | None = field(default=None, init=False)
    captured: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        valid = isinstance(self.config, dict) and isinstance(
            self.config.get("entities"), list
        )
        if not valid or any(
            not isinstance(value, str) for value in self.config["entities"]
        ):
            raise ToolFailure(
                "INVALID_SNAPSHOT",
                "Group snapshot requires its saved membership list",
                status="not_started",
            )
        for key in ("name", "icon"):
            if self.config.get(key) is not None and not isinstance(
                self.config[key], str
            ):
                raise ToolFailure(
                    "INVALID_SNAPSHOT",
                    "Group snapshot has invalid text fields",
                    status="not_started",
                )
        if "all" in self.config and type(self.config["all"]) is not bool:
            raise ToolFailure(
                "INVALID_SNAPSHOT",
                "Group snapshot has invalid aggregation mode",
                status="not_started",
            )

    async def current(self) -> dict[str, Any] | None:
        identifier = group_identity(self.reference, self.config)
        await authorize_actor(self.service.actor, self.service.authorize)
        self.component = records.component(self.service.hass)
        self.owner = records.target(self.component, "group." + identifier)
        self.full_before = records.group_snapshot(self.owner)
        self.captured = True
        return records.public_snapshot(self.full_before)

    async def comparison(self) -> dict[str, Any] | None:
        value = await self.current()
        return (
            {key: value[key] for key in self.config if key in value}
            if value is not None
            else None
        )

    def _payload(self) -> dict[str, Any]:
        identifier = group_identity(self.reference, self.config)
        payload = {"object_id": identifier}
        for key in ("name", "entities", "icon", "all"):
            if key in self.config and self.config[key] is not None:
                payload[key] = self.config[key]
        fields = records.validate_service(self.service.hass, "set", payload)
        if "icon" in self.config and self.config["icon"] is None:
            # Core's Group.set_icon explicitly accepts None; its service schema
            # cannot express clearing. The saved value stays a real owner call.
            fields["icon"] = None
        return fields

    async def apply(self, expected: Any) -> dict[str, Any]:
        if not self.captured or not json_values_equal(
            records.public_snapshot(self.full_before), expected
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The group safety image is not the admitted before-image",
                status="not_started",
            )
        identifier = group_identity(self.reference, self.config)
        entity_id = "group." + identifier
        fields = self._payload()
        async with registry_update_lock("group", entity_id):
            await self.service._admit(
                self.component, entity_id, self.owner, self.full_before
            )
            await await_owned_io(
                self.service._set_owned(
                    self.component, entity_id, self.owner, self.full_before, fields
                )
            )
        warnings = [
            "Service-group configuration is restored for this running instance. YAML and startup configuration remain authoritative after reload or restart."
        ]
        if "all" not in self.config:
            warnings.append(
                "This older snapshot did not record aggregation mode; the existing mode is preserved, or Core's default is used when recreating a group."
            )
        return {"runtime_only": True, "warnings": warnings}
