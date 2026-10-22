"""Entry-scoped policy document operations through the native settings owner."""

from __future__ import annotations

from functools import partial
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.policy.document_values import (
    PolicyCaller,
    policy_write_warnings,
    prepare_policy_document,
)
from ha_mcp.policy.model import Policy
from ha_mcp.policy.persistence import load_policy
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from ..native_server_policy import NativePolicy
from ..native_server_policy import load_policy as load_native_policy
from .registry_access import authorize_actor


class NativePolicyDocument:
    def __init__(
        self, manager: Any, actor: CallContext, *, developer: bool = False
    ) -> None:
        self.manager, self.actor, self.developer = manager, actor, developer
        self.caller = (
            PolicyCaller("ha_dev_manage_settings", "get_policy", "set_policy")
            if developer
            else PolicyCaller("ha_manage_security_policy", "get", "set")
        )

    async def guard(self, writes: bool = False) -> None:
        await authorize_actor(self.actor, self.manager.backend.authorize)
        if writes:
            check_mutation_admission(self.actor)
        self._check_policy(self.manager.policy, writes)

    def _check_policy(self, policy: NativePolicy, writes: bool) -> None:
        gate = "enable_dev_mode" if self.developer else "enable_security_policy_tool"
        if not policy.flag(gate):
            raise ToolFailure(
                "FEATURE_DISABLED", f"{gate} is disabled", status="not_started"
            )
        if writes:
            if self.actor.read_only or policy.read_only:
                raise ToolFailure(
                    "READ_ONLY", "Policy writes are disabled", status="not_started"
                )
            if self.developer and not policy.flag("dev_tools_security_policy_access"):
                raise ToolFailure(
                    "AUTH_INSUFFICIENT_PERMISSIONS",
                    "Dev tools security policy access is disabled; only a person may enable it in settings or the environment",
                    status="not_started",
                )

    def _before_write(self) -> None:
        # The worker may wait behind an administrator's settings change.
        # This loader is pure and does not reacquire the held file lock.
        self._check_policy(
            load_native_policy(self.manager.settings_ui.directory), writes=True
        )

    def live(self) -> bool:
        return bool(
            self.manager.policy.flag("enable_tool_security_policies")
            and self.manager.approval_gate is not None
        )

    async def get(self) -> dict[str, Any]:
        await self.guard()
        policy = await await_owned_io(
            self.manager.settings_ui._io(
                load_policy, self.manager.settings_ui.directory
            )
        )
        await self.guard()
        return {
            "success": True,
            "data": {
                "policy": policy.model_dump(mode="json"),
                "policies_enabled": self.manager.policy.flag(
                    "enable_tool_security_policies"
                ),
                "policies_live": self.live(),
            },
        }

    async def set(
        self, policy: dict[str, Any] | None, expected_version: int | None
    ) -> dict[str, Any]:
        await self.guard(True)
        try:
            new, expected = prepare_policy_document(
                policy, expected_version, self.caller
            )
        except ToolFailure as failure:
            failure.status = "not_started"
            raise
        ui = self.manager.settings_ui
        current = await await_owned_io(ui._io(load_policy, ui.directory))
        effective_version = current.version if expected is None else expected
        warnings = policy_write_warnings(
            new,
            current,
            effective_version,
            policies_enabled=self.manager.policy.flag("enable_tool_security_policies"),
            policies_live=self.live(),
        )
        body = new.model_dump(mode="json", exclude_unset=True)
        # The native owner also compares a missing-version write against the
        # state read here, instead of silently overwriting a later edit.
        body["version"] = effective_version
        if expected is None:
            warnings.append(
                "No version supplied; compared against the native policy version read at the start of this call."
            )

        async def persist() -> Any:
            await self.guard(True)
            return await ui._io(
                partial(ui._save_policy, before_write=self._before_write), body
            )

        try:
            result, changed = await await_owned_io(persist())
        except ValueError as error:
            payload = create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER, str(error)
            )
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                str(error),
                data=payload,
                status="not_started",
            ) from error
        except OSError as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Policy persistence did not complete normally; read the policy before retrying",
                status="outcome_unknown",
            ) from error
        if "current_version" in result:
            await self.guard()
            payload = create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"policy version mismatch — reload with {self.caller.get_call} before saving",
                context={
                    "current_version": result["current_version"],
                    "current_policy": result["current_policy"],
                },
            )
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                payload["error"]["message"],
                data=payload,
                status="not_started",
            )
        return await self._finish(result, changed, current, new, warnings)

    async def _finish(
        self,
        result: dict[str, Any],
        changed: bool,
        current: Policy,
        new: Policy,
        warnings: list[str],
    ) -> dict[str, Any]:
        if changed:
            try:
                self.manager.approval_gate.queue.clear_remember_cache()
            except Exception:  # noqa: BLE001 - persistence has already committed
                warnings.append(
                    "The policy was saved, but remembered approvals could not be cleared; inspect the approval engine before retrying."
                )
        try:
            await self.manager.refresh_policy()
            await self.guard()
        except (PermissionError, ToolFailure, ValueError, OSError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Policy was saved, but live authority or refresh could not be verified",
                data={
                    "success": False,
                    "error": {
                        "code": "OUTCOME_UNKNOWN",
                        "message": "Policy saved; read it before retrying",
                    },
                    "write_committed": True,
                    "version": result["version"],
                },
                status="outcome_unknown",
            ) from error
        response = {
            "success": True,
            "data": {
                "version": result["version"],
                "rules_changed": current.rules != new.rules,
            },
        }
        if warnings:
            response["warnings"] = warnings
        return response

    async def manage(
        self,
        action: str,
        policy: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        return (
            await self.get()
            if action == "get"
            else await self.set(policy, expected_version)
        )
