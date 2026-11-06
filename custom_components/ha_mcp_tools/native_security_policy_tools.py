"""Bind policy-document editing to the actual entry-scoped settings owner."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.security_policy_tools import SecurityPolicyFacade
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.policy_document import NativePolicyDocument


def security_policy_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        return await SecurityPolicyFacade(
            NativePolicyDocument(manager, actor)
        ).ha_manage_security_policy(**arguments)

    return define_tool(
        manager.backend,
        SecurityPolicyFacade,
        "ha_manage_security_policy",
        handler=invoke,
        source_module="tools_security_policy",
        tags=frozenset({"System"}),
        annotations=write_hints(
            "Manage Security Policy",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
