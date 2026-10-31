"""One public policy-document contract for native and legacy owners."""

from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class SecurityPolicyFacade:
    def __init__(self, service: Any) -> None:
        self.service = service

    async def ha_manage_security_policy(
        self,
        action: Annotated[
            Literal["get", "set"],
            Field(
                description="get: read the whole policy document. set: replace it with the supplied one."
            ),
        ],
        policy: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description="set: the full policy object {rule_effect, wait_seconds, approval_ttl_minutes, event_decisions_enabled, rules, version}. rule_effect 'require_approval' (default): a matching rule gates the call; 'allow': a matching rule approves the call and every other call requires approval",
            ),
        ] = None,
        expected_version: Annotated[
            int | None,
            Field(
                default=None,
                description="set: the version from your last get, for optimistic-concurrency safety (else the policy's own version field is used)",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage the tool security policy that gates high-stakes tool calls behind user approval.

        When NOT to use: this tool cannot see or decide pending approval
        requests — listing, approving, and denying them is developer-mode
        only (ha_dev_manage_server). For ordinary Home Assistant entity,
        automation, or dashboard work use the ha_config_* tools.

        When to use: to read the current policy, to add or remove per-tool
        approval rules and their matching conditions, to switch the rule list
        between gating and approving the calls it matches, and to change the
        approval wait time or how long an approval is remembered.

        Caveats: set replaces the WHOLE document, so send back an edited
        copy of what get returned, not a fragment. An omitted field is not
        left alone, it reverts to its default: dropping
        event_decisions_enabled switches off approving from Home Assistant
        events, and the response says so in a warning when it does, while
        dropping rule_effect from an allow list is refused. Writes are
        version-guarded: a concurrent edit is rejected instead of
        silently overwritten. Rule edits apply to the running server
        immediately and can remove approval gates. Whether this tool itself
        is registered is a separate setting that takes effect on restart.

        EXAMPLES:
        ha_manage_security_policy("get")
        ha_manage_security_policy("set", policy={"wait_seconds": 60, "approval_ttl_minutes": 5, "event_decisions_enabled": False, "rules": [{"tool_name": "ha_call_service"}], "version": 3})
        """
        result: dict[str, Any] = await self.service.manage(
            action=action, policy=policy, expected_version=expected_version
        )
        return result
