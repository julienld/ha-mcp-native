"""Single public contract for native owners and isolated legacy adapters."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class DevFacade:
    def __init__(self, service: Any) -> None:
        self.service = service

    async def ha_dev_manage_settings(
        self,
        action: Annotated[
            Literal[
                "list",
                "set",
                "reset",
                "list_tools",
                "set_tool",
                "get_policy",
                "set_policy",
                "get_backup_config",
                "set_backup_config",
            ],
            Field(
                description="Server-settings matrix: list / set / reset. Tools tab: list_tools / set_tool (enable-disable-pin, LLM-API, security gate). Security policies: get_policy / set_policy. Auto-backup config: get_backup_config / set_backup_config."
            ),
        ],
        setting: Annotated[
            str | None,
            Field(default=None, description="Setting name (required for set/reset)"),
        ] = None,
        value: Annotated[
            bool | int | float | str | None,
            Field(default=None, description="New value (required for set)"),
        ] = None,
        tool: Annotated[
            str | None,
            Field(default=None, description="Tool name (required for set_tool)"),
        ] = None,
        state: Annotated[
            Literal["enabled", "disabled", "pinned"] | None,
            Field(
                default=None, description="set_tool: enable, disable, or pin the tool"
            ),
        ] = None,
        llm_api: Annotated[
            bool | None,
            Field(
                default=None,
                description="set_tool: expose the tool to HA conversation agents (effective only on the embedded custom-component server)",
            ),
        ] = None,
        gated: Annotated[
            bool | None,
            Field(
                default=None,
                description="set_tool: toggle the tool's unconditional security-policy rule. In a require-approval list gated=true adds it (every call needs approval); in an allow list that rule approves the tool, so gated=true removes it. Conditional rules are left in place and still apply. gated=true is refused while an allow list's unconditional '*' rule approves every tool",
            ),
        ] = None,
        policy: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description="set_policy: the full policy object {rule_effect, wait_seconds, approval_ttl_minutes, event_decisions_enabled, rules, version, schema_version}. Replaces the WHOLE document: a field you omit reverts to its default, so send back an edited copy of get_policy rather than a fragment (omitting event_decisions_enabled switches the event-bus approval channel off; omitting rule_effect while the stored policy is an allow list is refused)",
            ),
        ] = None,
        expected_version: Annotated[
            int | None,
            Field(
                default=None,
                description="set_policy: the version from your last get_policy, for optimistic-concurrency safety (else the policy's own version field is used)",
            ),
        ] = None,
        backup: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description="set_backup_config: {field: value} of auto-backup settings to change (see get_backup_config for field names)",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage ha-mcp server settings and the Tools/Policies/Backups surfaces (developer mode).

        Drives everything the web settings UI can change: the Server
        Settings matrix, the Tools tab, the Tool Security Policies editor,
        and agent-editable backup config. Snapshot Actions, Backup Read
        Only, snapshot deletion and its minimum age are human-only controls. Use ha_dev_manage_server for
        the live approval queue and to restart.

        When NOT to use: for HA entity/automation configuration use the
        ha_config_* tools.

        Caveats: enable/disable/pin and most server settings take effect
        only after a restart (ha_dev_manage_server action="restart");
        LLM-API exposure, security gates, and policy edits apply live.
        Env-pinned settings/tools are read-only until the env var is unset.
        These actions can flip security-sensitive state — treat with the
        same care as editing the web UI. The policy-override surfaces
        (set_policy, set_tool with gated=, and set/reset of
        enable_tool_security_policies) additionally require the
        'dev_tools_security_policy_access' setting, which is off by
        default; reads are always available. That setting itself is never
        writable by these tools — change it in the web settings UI or via
        HAMCP_DEV_SECURITY_POLICY_ACCESS.

        EXAMPLES:
        ha_dev_manage_settings("list_tools")
        ha_dev_manage_settings("set_tool", tool="ha_write_file", state="disabled")
        ha_dev_manage_settings("set_tool", tool="ha_call_service", gated=True)
        ha_dev_manage_settings("get_policy")
        ha_dev_manage_settings("set_backup_config", backup={"enable_auto_backup": False})
        """
        result: dict[str, Any] = await self.service.manage_settings(
            action=action,
            setting=setting,
            value=value,
            tool=tool,
            state=state,
            llm_api=llm_api,
            gated=gated,
            policy=policy,
            expected_version=expected_version,
            backup=backup,
        )
        return result

    async def ha_dev_manage_server(
        self,
        action: Annotated[
            Literal[
                "info", "update_source", "restart", "list_pending", "approve", "deny"
            ],
            Field(
                description="info: deployment/version report; update_source: point the ha_mcp_tools component's separate in-process server at a pip spec (or clear it) and reinstall it; restart: restart this server; list_pending: list tool calls blocked on a security-policy approval; approve / deny: decide one blocked call by token"
            ),
        ],
        pip_spec: Annotated[
            str | None,
            Field(
                default=None,
                description="Explicit pip requirement for update_source — a version pin (ha-mcp==7.9.0) or a GitHub tarball URL such as https://github.com/homeassistant-ai/ha-mcp/archive/refs/pull/<PR>/head.tar.gz. The bare name 'clear' (case-insensitive) is reserved: it clears the override and returns to the server the component release pins instead of being treated as a requirement. An empty string also clears but some MCP clients mangle it in transit — prefer 'clear'.",
            ),
        ] = None,
        token: Annotated[
            str | None,
            Field(
                default=None, description="Approval token (required for approve/deny)"
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage the running ha-mcp server itself (developer mode).

        When NOT to use: to restart Home Assistant use ha_restart; to
        update Home Assistant Apps (add-ons) or HACS packages use
        ha_manage_app / ha_manage_hacs.

        Caveats: update_source changes ONLY the ha_mcp_tools custom
        component's separate in-process server entry — it never updates
        the app (add-on), Docker, standalone, or PyPI server that may be
        serving this connection (update those via ha_manage_app /
        docker pull / pip). In embedded mode that entry IS this server,
        so the update self-interrupts; elsewhere this connection is
        untouched and keeps its current version. Success means the
        entry's options were applied — the component then reinstalls in
        the background, which can take minutes and can still fail
        (check HA logs). update_source requires the component's
        in-process server entry to exist. restart interrupts this MCP
        connection in embedded and app deployments (the reply
        arrives just before the server goes down) and supports those
        two deployments only (standalone processes must be restarted
        externally). approve/deny require the separate
        'dev_tools_security_policy_access' setting, off by default.

        EXAMPLES:
        ha_dev_manage_server("update_source", pip_spec="https://github.com/homeassistant-ai/ha-mcp/archive/refs/pull/1234/head.tar.gz")
        ha_dev_manage_server("update_source", pip_spec="clear")
        ha_dev_manage_server("approve", token="abc123")
        """
        result: dict[str, Any] = await self.service.manage_server(
            action=action, pip_spec=pip_spec, token=token
        )
        return result
