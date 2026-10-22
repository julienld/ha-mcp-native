"""Developer controls over the actual native settings directory and owners."""

from __future__ import annotations

import os
from typing import Any

from ha_mcp.config_registry import (
    ADVANCED_SETTINGS_FIELDS,
    BETA_FEATURE_FIELDS,
    FEATURE_FLAG_FIELDS,
    SETTING_BOUNDS,
)
from ha_mcp.config_settings import Settings
from ha_mcp.renamed_tools import current_tool_name
from ha_mcp.runtime.dev_values import (
    _HUMAN_ONLY_SETTINGS,
    HUMAN_BACKUP_CONTROLS,
    _coerce_setting_value,
)
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_write_lock import config_file_lock
from ha_mcp.utils.owned_executor import await_owned_io

from ..native_server_policy import (
    _read_object,
    environment_name,
    load_policy,
    setting_value,
)
from ..native_settings_fields import ADVANCED_SETTINGS, FEATURE_FLAGS
from .developer_store import patch_tool, require_dev_write
from .policy_document import NativePolicyDocument
from .registry_access import authorize_actor


class NativeDeveloperSettings:
    def __init__(self, manager: Any, actor: CallContext) -> None:
        self.manager, self.actor = manager, actor
        self.ui = manager.settings_ui

    async def guard(self, writes: bool = False, *, security: bool = False) -> None:
        await authorize_actor(self.actor, self.manager.backend.authorize)
        if not self.manager.policy.flag("enable_dev_mode"):
            raise ToolFailure(
                "FEATURE_DISABLED", "Developer mode is disabled", status="not_started"
            )
        if writes:
            check_mutation_admission(self.actor)
            if self.actor.read_only or self.manager.policy.read_only:
                raise ToolFailure(
                    "READ_ONLY", "Developer writes are disabled", status="not_started"
                )
        if security and not self.manager.policy.flag(
            "dev_tools_security_policy_access"
        ):
            raise ToolFailure(
                "AUTH_INSUFFICIENT_PERMISSIONS",
                "Dev tools security policy access is disabled; enable it in the UI or environment",
                status="not_started",
            )

    async def _call(
        self, function: Any, *args: Any, writes: bool = False, security: bool = False
    ) -> dict[str, Any]:
        async def owned() -> dict[str, Any]:
            await self.guard(writes, security=security)
            try:
                result: dict[str, Any] = await self.ui._io(function, *args)
            except ToolFailure as error:
                if error.status is None:
                    error.status = "not_started"
                raise
            except ValueError as error:
                raise ToolFailure(
                    "VALIDATION_INVALID_PARAMETER", str(error), status="not_started"
                ) from error
            except OSError as error:
                raise ToolFailure(
                    "OUTCOME_UNKNOWN" if writes else "INTERNAL_ERROR",
                    "Settings I/O failed; read the current state before retrying",
                    status="outcome_unknown" if writes else "not_started",
                ) from error
            return await self._finish_io(result, writes, security)

        return await await_owned_io(owned())

    async def _finish_io(
        self,
        result: dict[str, Any],
        writes: bool,
        security: bool,
    ) -> dict[str, Any]:
        if (
            result.pop("_gates_changed", False)
            and self.manager.approval_gate is not None
        ):
            try:
                self.manager.approval_gate.queue.clear_remember_cache()
            except Exception:  # noqa: BLE001 - persistence already committed
                result.setdefault("warnings", []).append(
                    "Policy saved, but remembered approvals could not be cleared; inspect the live queue before retrying."
                )
        try:
            await self.manager.refresh_policy()
            # A successful write may deliberately disable Dev or enable RO.
            # Revalidate the principal without mislabelling its own commit.
            if writes:
                await authorize_actor(self.actor, self.manager.backend.authorize)
            else:
                await self.guard(security=security)
        except (ToolFailure, PermissionError, ValueError, OSError) as error:
            if writes:
                raise ToolFailure(
                    "OUTCOME_UNKNOWN",
                    "Settings were saved but live authority/refresh could not be verified",
                    data={
                        "write_committed": True,
                        "read_back": "list, list_tools, get_policy or get_backup_config",
                    },
                    status="outcome_unknown",
                ) from error
            raise
        return result

    def _rows(self) -> dict[str, Any]:
        policy = load_policy(self.ui.directory)
        kinds = {name: kind for name, _env, kind in FEATURE_FLAG_FIELDS}
        kinds.update({name: kind for name, _env, kind, *_ in ADVANCED_SETTINGS_FIELDS})
        rows = []
        for name in (*FEATURE_FLAGS, *ADVANCED_SETTINGS):
            env, kind = environment_name(name), kinds[name]
            row = {
                "setting": name,
                "env_var": env,
                "type": kind.__name__,
                "registry": "features" if name in FEATURE_FLAGS else "advanced",
                "value": policy.flag(name)
                if kind is bool
                else kind(setting_value(policy.flags, name)),
                "origin": "env"
                if env in os.environ
                else "file"
                if name in policy.flags
                else "default",
                "editable": env not in os.environ,
            }
            if row["editable"] and name in _HUMAN_ONLY_SETTINGS:
                row.update(editable=False, locked_reason="web_ui_or_env_only")
            elif (
                row["editable"]
                and name == "enable_tool_security_policies"
                and not policy.flag("dev_tools_security_policy_access")
            ):
                row.update(editable=False, locked_reason="policy_access_required")
            if name in SETTING_BOUNDS:
                row["min"], row["max"] = SETTING_BOUNDS[name]
            rows.append(row)
        return {
            "success": True,
            "data": {
                "settings": rows,
                "is_addon": False,
                "is_embedded": True,
                "note": "Only native runtime settings are listed. Legacy network/client settings do not configure this Core runtime. Some catalogue changes require an integration reload.",
            },
        }

    def _setting(self, action: str, name: str | None, value: Any) -> dict[str, Any]:
        if name in _HUMAN_ONLY_SETTINGS or name in HUMAN_BACKUP_CONTROLS:
            raise ToolFailure(
                "AUTH_INSUFFICIENT_PERMISSIONS",
                f"{name} is human-only; change it in the UI or environment",
                status="not_started",
            )
        if name not in (*FEATURE_FLAGS, *ADVANCED_SETTINGS):
            raise ValueError(f"Unknown or unsupported native setting: {name!r}")
        assert name is not None
        if environment_name(name) in os.environ:
            raise ValueError(f"Environment-controlled setting: {name}")
        require_dev_write(
            self.ui.directory, security=name == "enable_tool_security_policies"
        )
        if action == "reset":
            return self._reset(name)
        kinds = {field: kind for field, _env, kind in FEATURE_FLAG_FIELDS}
        kinds.update(
            {field: kind for field, _env, kind, *_ in ADVANCED_SETTINGS_FIELDS}
        )
        coerced = _coerce_setting_value(name, value, kinds[name])
        if (
            name in BETA_FEATURE_FIELDS
            and coerced
            and not load_policy(self.ui.directory).flag("enable_beta_features")
        ):
            raise ValueError("Enable beta features before enabling a beta sub-feature")

        def before_write() -> None:
            effective = require_dev_write(
                self.ui.directory,
                security=name == "enable_tool_security_policies",
            )
            if environment_name(name) in os.environ:
                raise ValueError(f"Environment-controlled setting: {name}")
            if (
                name in BETA_FEATURE_FIELDS
                and coerced
                and not effective.flag("enable_beta_features")
            ):
                raise ValueError(
                    "Enable beta features before enabling a beta sub-feature"
                )
            self.ui._validate_bps_flags({name: coerced})

        saved = (
            self.ui._save_features(
                {"flags": {name: coerced}}, before_write=before_write
            )
            if name in FEATURE_FLAGS
            else self.ui._save_advanced({name: coerced}, before_write=before_write)
        )
        return {
            "success": True,
            "data": {
                "setting": name,
                "value": coerced,
                "mode": "file",
                "restart_required": saved["restart_required"],
            },
        }

    def _reset(self, name: str) -> dict[str, Any]:
        from ha_mcp.settings_ui._persistence import _atomic_write_json

        self.ui.directory.mkdir(parents=True, exist_ok=True)
        with config_file_lock(self.ui.directory):
            require_dev_write(
                self.ui.directory,
                security=name == "enable_tool_security_policies",
            )
            if environment_name(name) in os.environ:
                raise ValueError(f"Environment-controlled setting: {name}")
            default = Settings.model_fields[name].default
            self.ui._validate_bps_flags({name: default})
            path = self.ui.directory / "feature_flags.json"
            values = _read_object(path)
            had_override = name in values
            values.pop(name, None)
            if had_override:
                _atomic_write_json(path, values)
        return {
            "success": True,
            "data": {
                "setting": name,
                "removed_override": had_override,
                "restart_required": had_override,
            },
        }

    def _backup(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        if values is None:
            rows = self.ui._backup_config_fields()
            for row in rows:
                if row["field"] in HUMAN_BACKUP_CONTROLS:
                    row.update(editable=False, locked_reason="web_ui_or_env_only")
            return {"success": True, "data": {"fields": rows, "is_addon": False}}
        if HUMAN_BACKUP_CONTROLS.intersection(values):
            raise ToolFailure(
                "AUTH_INSUFFICIENT_PERMISSIONS",
                "Snapshot capabilities and deletion limits are human-only controls",
                status="not_started",
            )
        require_dev_write(self.ui.directory)
        saved = self.ui._save_backup_config(
            values,
            before_write=lambda: require_dev_write(self.ui.directory),
        )
        return {"success": True, "data": saved}

    async def _set_tool(
        self,
        tool: str | None,
        state: str | None,
        llm_api: bool | None,
        gated: bool | None,
    ) -> dict[str, Any]:
        name = current_tool_name(tool or "")
        if name not in self.manager.engine.tools or not any(
            value is not None for value in (state, llm_api, gated)
        ):
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Choose a known tool from list_tools and at least one of state, llm_api or gated",
                status="not_started",
            )

        def persist() -> dict[str, Any]:
            result, changed = patch_tool(self.ui.directory, name, state, llm_api, gated)
            # Return the cache action to the owning HA loop.
            result["_gates_changed"] = changed
            return result

        result = await self._call(persist, writes=True, security=gated is not None)
        return result

    async def manage_settings(self, action: str, **args: Any) -> dict[str, Any]:
        await self.guard()
        if action in {"get_policy", "set_policy"}:
            owner = NativePolicyDocument(self.manager, self.actor, developer=True)
            return (
                await owner.get()
                if action == "get_policy"
                else await owner.set(args.get("policy"), args.get("expected_version"))
            )
        if action == "list":
            return await self._call(self._rows)
        if action in {"set", "reset"}:
            return await self._call(
                self._setting,
                action,
                args.get("setting"),
                args.get("value"),
                writes=True,
                security=args.get("setting") == "enable_tool_security_policies",
            )
        if action == "list_tools":
            from .developer_tools import tool_rows

            return await self._call(tool_rows, self.manager)
        if action == "set_tool":
            return await self._set_tool(
                args.get("tool"),
                args.get("state"),
                args.get("llm_api"),
                args.get("gated"),
            )
        if action == "get_backup_config":
            return await self._call(self._backup)
        if action == "set_backup_config" and isinstance(args.get("backup"), dict):
            return await self._call(self._backup, args["backup"], writes=True)
        raise ToolFailure(
            "VALIDATION_INVALID_PARAMETER",
            "Invalid action or missing backup object",
            status="not_started",
        )
