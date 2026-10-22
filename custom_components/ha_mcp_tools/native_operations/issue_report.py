"""Collect a reviewable report from the actual Core and native manager owners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from ha_mcp import __version__
from ha_mcp.errors import create_validation_error
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.runtime.redaction import (
    known_secret_values,
    scrub_obj,
    scrub_text,
    with_serialized_variants,
)
from ha_mcp.runtime.report_environment import _CONFIG_TOGGLE_FIELDS, _detect_platform
from ha_mcp.runtime.report_rendering import render_report
from ha_mcp.runtime.report_templates import _ReportText, _sanitize_log_text
from ha_mcp.tool_api import CallContext
from ha_mcp.tools.coercion import ANSI_ESCAPE_RE, parse_string_list_param
from ha_mcp.tools.server_version import detect_installed_version, version_mismatch
from ha_mcp.utils.owned_executor import await_owned_io

from ..const import (
    COMPONENT_VERSION,
    DATA_ADMIN_TOKEN,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    OPT_WEBHOOK_AUTH,
)
from ..native_server_policy import setting_value
from .log_files import NativeLogFiles
from .registry_access import authorize_actor


@dataclass(slots=True)
class NativeIssueReport:
    manager: Any
    actor: CallContext

    @property
    def hass(self) -> Any:
        return self.manager.hass

    @property
    def authorize(self) -> Any:
        return self.manager.backend.authorize

    def _secrets(self) -> tuple[str, ...]:
        values = [
            self.manager.entry.data.get(key)
            for key in (
                DATA_ADMIN_TOKEN,
                DATA_OAUTH_CLIENT_SECRET,
                DATA_OAUTH_SIGNING_KEY,
                DATA_SECRET_PATH,
                DATA_WEBHOOK_ID,
            )
        ]
        return with_serialized_variants(
            (
                *known_secret_values(),
                *(
                    value
                    for value in values
                    if isinstance(value, str) and len(value) >= 8
                ),
            )
        )

    def _config(self) -> dict[str, Any]:
        from ha_mcp.config_settings import Settings

        policy = self.manager.policy
        flags = {
            name: policy.flag(name)
            if isinstance(Settings.model_fields[name].default, bool)
            else setting_value(policy.flags, name)
            for name in _CONFIG_TOGGLE_FIELDS
            if name != "enable_websocket"
        }
        flags["read_only_mode"] = policy.read_only
        requested = {
            name for name, state in policy.tool_states.items() if state == "disabled"
        }
        enabled = self.manager.engine.enabled_tools or set()
        ignored = sorted(requested & enabled)
        return {
            **flags,
            "disabled_tools_count": len(requested),
            "pinned_tools_count": len(policy.pinned),
            "custom_instructions_set": bool(policy.custom_instructions.strip()),
            "tool_config_status": "available",
            "requested_disabled_tools_count": len(requested),
            "effective_disabled_tools_count": len(requested - set(ignored)),
            "ignored_disabled_tools": ignored,
            "tool_config_warnings": [],
        }

    def _core(self) -> dict[str, Any]:
        from homeassistant.const import __version__ as core_version

        data = self.hass.data.get("ha_mcp_tools", {})
        owner = data.get("file_access_owner")
        tools = (
            "set up (File & YAML owner active)"
            if owner is not None and owner.active
            else "not set up — add the separate File & YAML Tools entry"
        )
        supervisor: dict[str, str] = {"none": "true"}
        if "hassio" in self.hass.config.components:
            try:
                from homeassistant.components.hassio import get_info

                info = get_info(self.hass)
                supervisor = {
                    "supervisor_version": str(info.get("supervisor") or "unknown"),
                    "host_os": str(info.get("operating_system") or "unknown"),
                }
            except Exception as error:  # noqa: BLE001 - a missing diagnostic stays explicit
                supervisor = {"error": type(error).__name__}
        policy = self.manager.policy.security_policy
        return {
            "ha_mcp_version": __version__,
            "component_version": COMPONENT_VERSION,
            "tools_entry_status": tools,
            "server_entry_status": f"added ({self.manager.entry.state.value}), native runtime, webhook auth: {self.manager.entry.options.get(OPT_WEBHOOK_AUTH, 'none')}",
            "installation_method": "native",
            "connection_status": "In-process Core",
            "home_assistant_version": core_version,
            "entity_count": len(self.hass.states.async_all()),
            "location_name": self.hass.config.location_name,
            "time_zone": self.hass.config.time_zone,
            "supervisor": supervisor,
            "mcp_transport": "Home Assistant native (HTTP or Assist)",
            "mcp_client_info": {},
            "mcp_client_host": {},
            "http_user_agent": "",
            "websockets_dependency": {
                "not_used": "Native tools use Core owners without an outbound WebSocket client"
            },
            "instance": {
                "instance_id": self.manager.settings_ui.worker_id,
                "scope": "native config entry",
            },
            "tool_policy": f"{policy.rule_effect}; {len(policy.rules)} rule(s)"
            if policy is not None
            else "not configured",
        }

    async def report(
        self,
        report_type: str = "runtime_bug",
        title: str | None = None,
        description: str | None = None,
        user_prompt: str | None = None,
        user_comment: str | None = None,
        tool_calls: str | None = None,
        ai_model: str | None = None,
        client_app: str | None = None,
        tool_call_count: int = 10,
        fields: str | list[str] | None = None,
        ctx: Any = None,
    ) -> dict[str, Any]:
        try:
            selected = (
                parse_string_list_param(fields, "fields", allow_csv=True)
                if fields is not None
                else None
            )
        except ValueError as error:
            raise_failure(create_validation_error(str(error), parameter="fields"))
        await authorize_actor(self.actor, self.authorize)
        await self.manager.refresh_policy()
        await authorize_actor(self.actor, self.authorize)
        diagnostics = self._core()
        toggles = self._config()
        diagnostics.update(
            config_toggles=toggles,
            ignored_disabled_tools=toggles["ignored_disabled_tools"],
            tool_config_warnings=toggles["tool_config_warnings"],
            tool_config_status=toggles["tool_config_status"],
        )
        records = [
            {
                "tool_name": record["tool"],
                "success": record["status"] == "confirmed",
                "status": record["status"],
                "operation_id": operation_id,
                "timestamp": "not recorded",
                "execution_time_ms": None,
                "error_message": ""
                if record["status"] == "confirmed"
                else record["status"],
            }
            for operation_id, record in list(self.manager.engine.operations.items())[
                -tool_call_count:
            ]
            if record["tool"] != "ha_report_issue"
        ]
        diagnostics["recent_log_source"] = (
            "native engine operation records; arguments, timestamps and durations are not recorded"
        )
        diagnostics["startup_log_status"] = (
            "not collected by the native runtime; the current bounded Core log is reported separately"
        )
        secrets = self._secrets()
        try:
            page = await NativeLogFiles(self).error_log(500, 0)
            core_log = page.text
        except Exception as error:  # noqa: BLE001 - report remains useful without logs
            core_log = f"(Core error log unavailable: {type(error).__name__})"

        def build() -> dict[str, Any]:
            def scrub(text: str | None) -> str | None:
                return (
                    _sanitize_log_text(scrub_text(text, secrets))
                    if text is not None
                    else None
                )

            text = _ReportText(
                **{
                    name: scrub(value)
                    for name, value in {
                        "title": title,
                        "description": description,
                        "user_prompt": user_prompt,
                        "user_comment": user_comment,
                        "tool_calls": tool_calls,
                        "ai_model": ai_model,
                        "client_app": client_app,
                    }.items()
                }
            )
            installed = detect_installed_version()
            diagnostics.update(
                platform=_detect_platform(),
                installed_version=installed,
                version_mismatch=version_mismatch(installed),
            )
            logs = scrub_obj(records, secrets)
            cleaned = scrub(ANSI_ESCAPE_RE.sub("", core_log)) or ""
            result = render_report(
                scrub_obj(diagnostics, secrets),
                logs,
                [],
                "",
                cleaned[-5000:],
                report_type,
                text,
                selected,
            )
            result.setdefault("warnings", []).append(
                "Client application details are not inferred from Home Assistant credentials. Supply client_app only from the user's own answer. The generated report is not published."
            )
            return cast(dict[str, Any], scrub_obj(result, secrets))

        result: dict[str, Any] = await await_owned_io(
            self.hass.async_add_executor_job(build)
        )
        await authorize_actor(self.actor, self.authorize)
        return result
