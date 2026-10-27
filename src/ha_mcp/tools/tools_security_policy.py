"""Tool-security-policy editing as a regular MCP tool (issue #2148).

Hidden behind the ``enable_security_policy_tool`` setting — the toggle on
the Tool Security Policies tab of the web settings UI, or the
``ENABLE_SECURITY_POLICY_TOOL`` env var. When the flag is off (the
default) ``register_security_policy_tools`` registers nothing, so the tool
does not exist for MCP clients at all.

The live approval QUEUE stays out of reach here by design: this tool reads
and writes the policy document only. Listing, approving, and denying
pending approvals remain developer-mode actions (``ha_dev_manage_server``).

Feature Flag: Set ENABLE_SECURITY_POLICY_TOOL=true to enable this tool.
"""

import logging
from typing import Any, Literal

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..policy.editing import PolicyCaller, get_policy, set_policy
from ..runtime.security_policy_tools import SecurityPolicyFacade
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    register_tool_methods,
)
from .tool_hints import write_hints

logger = logging.getLogger(__name__)

# Feature flag - disabled by default; the toggle lives on the Tool Security
# Policies tab of the web settings UI, under the master policies switch.
FEATURE_FLAG = "ENABLE_SECURITY_POLICY_TOOL"

# How the shared policy-editing helpers spell this tool's own actions when
# they build user-facing messages.
_POLICY_CALLER = PolicyCaller("ha_manage_security_policy", "get", "set")


class LegacySecurityPolicyTools:
    """Read/write access to the tool security policy document."""

    def __init__(self, client: Any, server: Any) -> None:
        self._client = client
        # The live server object, passed by the registry. Used only to
        # report whether the approval engine is running (``policies_live``)
        # and to drop remembered approvals when the rules change — never to
        # reach the queue's contents.
        self._server = server

    async def manage(
        self,
        action: Literal["get", "set"],
        policy: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        try:
            if action == "get":
                return await get_policy(self._server)
            return await set_policy(
                policy, expected_version, _POLICY_CALLER, self._server
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 - legacy error classifier
            exception_to_structured_error(
                e,
                context={"action": action},
                suggestions=["Check server logs for details"],
            )
            return None


class SecurityPolicyTools(SecurityPolicyFacade, LegacySecurityPolicyTools):
    def __init__(self, client: Any, server: Any) -> None:
        LegacySecurityPolicyTools.__init__(self, client, server)
        SecurityPolicyFacade.__init__(self, self)

    ha_manage_security_policy = tool(
        name="ha_manage_security_policy",
        tags={"System"},
        annotations=write_hints(
            "Manage Security Policy",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(log_tool_usage(legacy_errors(SecurityPolicyFacade.ha_manage_security_policy)))


def register_security_policy_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the security-policy tool when the feature flag is on.

    Registers NOTHING unless ``enable_security_policy_tool`` is set — the
    tool is invisible to MCP clients by default. Mirrors the early-return
    gate used by register_dashboard_screenshot_tools.
    """
    from ..config import get_global_settings

    # ``getattr`` with a False default, not attribute access: during an
    # in-process package update the cached settings singleton can predate
    # this field (issues #1783/#1785), and that stale read must mean "tool
    # off", never AttributeError.
    if not getattr(get_global_settings(), "enable_security_policy_tool", False):
        logger.debug(f"Security policy tool disabled (set {FEATURE_FLAG}=true)")
        return

    logger.warning(
        "ha_manage_security_policy is registered: connected MCP clients can "
        "read and rewrite the tool security policies, including removing "
        "approval gates."
    )
    # kwargs["server"], not .get(): the registry always passes it, and a
    # KeyError from a future call site that forgets is caught and logged
    # loudly by the registry — better than silently registering a tool that
    # cannot report whether the approval engine is live.
    register_tool_methods(mcp, SecurityPolicyTools(client, server=kwargs["server"]))
