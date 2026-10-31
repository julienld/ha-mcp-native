"""Native YAML editing binds live settings to the component's canonical editor."""

from __future__ import annotations

import posixpath
from typing import Any

from ha_mcp.runtime.skill_content import apply_skill_content
from ha_mcp.runtime.skill_files import read_skill_files
from ha_mcp.runtime.yaml_lookup import YamlLookup
from ha_mcp.runtime.yaml_values import (
    _YAML_PACKAGES_FLAG_BY_KEY,
    _YAML_SKILL_FILES,
    _append_yaml_result_warnings,
    _validate_yaml_action_and_content,
)
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from ..file_io import FileImage, FileWriteState
from ..file_views import _extract_yaml_views
from ..native_server_policy import setting_value
from ..yaml_edit_values import _parse_and_validate_yaml_path
from ..yaml_editing import (
    YamlEditor,
    _check_packages_key_gate,
    _classify_edit_yaml_target,
    _effective_extra_allowed_keys,
)
from .dashboard_storage import DashboardStorage
from .file_entry import NativeFileEntry
from .filesystem import NativeFilesystem


class NativeYamlRead:
    def __init__(self, manager: Any, actor: CallContext) -> None:
        self.files = NativeFilesystem(manager, actor)

    async def get(self, **arguments: Any) -> dict[str, Any]:
        await self.files.guard(False)
        result = await await_owned_io(YamlLookup(self.files).get(**arguments))
        await self.files.guard(False)
        return result


class NativeYamlConfig(NativeFileEntry):
    def __init__(self, manager: Any, actor: CallContext) -> None:
        super().__init__(manager, actor, feature="enable_yaml_config_editing")

    def _options(self) -> dict[str, Any]:
        value = str(
            setting_value(self.manager.policy.flags, "extra_yaml_write_keys") or ""
        )
        return {
            "extra_allowed_keys": sorted(
                {key.strip() for key in value.split(",") if key.strip()}
            ),
            "disabled_packages_keys": [
                key
                for key, flag in _YAML_PACKAGES_FLAG_BY_KEY.items()
                if not self.manager.policy.flag(flag)
            ],
        }

    async def edit_guard(
        self, file: str, yaml_path: str, action: str, writes: bool
    ) -> None:
        files = self.owner
        assert files is not None
        normalized = posixpath.normpath(file)
        await self.guard(writes)
        if not writes:
            return
        is_package, is_theme, target_error = await _classify_edit_yaml_target(
            files.hass, file, normalized
        )
        current = self._options()
        gate_error = _check_packages_key_gate(current, yaml_path, is_package)
        _kind, _parts, key_error = _parse_and_validate_yaml_path(
            yaml_path,
            is_package=is_package,
            is_theme=is_theme,
            extra_allowed_keys=_effective_extra_allowed_keys(files.hass, current),
        )
        if error := target_error or gate_error:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                str(error["error"]),
                data=error,
                status="not_started",
            )
        if key_error:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER", key_error, status="not_started"
            )
        if action != "remove" and yaml_path.startswith("lovelace.dashboards."):
            target = yaml_path.removeprefix("lovelace.dashboards.")
            storage = DashboardStorage(
                files.hass, self.actor, authorize=self.manager.backend.authorize
            )
            _name, existing = storage.resolve(target)
            if existing is not None and existing.mode == "storage":
                raise ToolFailure(
                    "VALIDATION_INVALID_PARAMETER",
                    f"A storage-mode dashboard already owns url_path '{target}'. Delete it or choose another path.",
                    status="not_started",
                )
        await self.guard(True)

    async def edit(
        self,
        yaml_path: str,
        action: str,
        content: str | None = None,
        file: str = "configuration.yaml",
        confirm_token: str | None = None,
        MandatoryBPS: bool = True,
        BestPracticeKey: str | None = None,
    ) -> dict[str, Any]:
        from homeassistant.core import Context

        _validate_yaml_action_and_content(action, content)
        files = await self._owner()
        normalized = posixpath.normpath(file)

        async def guard(writes: bool) -> None:
            await self.edit_guard(file, yaml_path, action, writes)

        async def capture(path: str, image: FileImage) -> None:
            await guard(True)
            views = await await_owned_io(
                files.hass.async_add_executor_job(
                    _extract_yaml_views, image.text or "", yaml_path
                )
            )
            if views.get("parse_error"):
                raise ToolFailure(
                    "BACKUP_CAPTURE_FAILED",
                    "The existing YAML cannot be captured",
                    status="not_started",
                )
            await self.manager.snapshots.file_backup(
                {
                    "domain": "yaml",
                    "backup_id": f"{path}::{yaml_path}",
                    "current_config": views["subtree"],
                    "source_tool": "ha_config_set_yaml",
                },
                self.actor,
            )

        result = await YamlEditor(files).edit(
            {
                "file": file,
                "action": action,
                "yaml_path": yaml_path,
                "content": content,
                "confirm_token": confirm_token,
                "require_confirm": self.manager.policy.flag("enable_yaml_edit_confirm"),
                **self._options(),
            },
            guard=guard,
            before_write=capture,
            context=Context(user_id=self.actor.user_id),
        )
        try:
            _append_yaml_result_warnings(
                result,
                action=action,
                top_key=yaml_path.split(".", 1)[0],
                normalized_target=normalized,
            )
            requested = MandatoryBPS and confirm_token is None
            enabled = self.manager.policy.flag("enable_mandatory_bps")
            values = await await_owned_io(
                files.hass.async_add_executor_job(
                    lambda: read_skill_files(
                        list(_YAML_SKILL_FILES) if requested else [], enabled=enabled
                    )
                )
            )
            apply_skill_content(
                result,
                values["content"],
                enabled=enabled,
                requested=requested,
                vendor_missing=values["vendor_missing"],
            )
            await self.guard(False)
            return result
        except BaseException as error:
            if result.get("written"):
                raise files._unknown(
                    FileWriteState(started=True, committed=True)
                ) from error
            raise
