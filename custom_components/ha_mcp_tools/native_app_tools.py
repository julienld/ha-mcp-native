"""App contracts bound to native Supervisor services; opaque calls are writes."""

from dataclasses import replace
from typing import Any

from ha_mcp.runtime.app_tools import AppTools
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolHandler
from ha_mcp.tools.tool_hints import read_only_hints, write_hints

from .native_operations.app_actions import app_operation_timeout
from .native_operations.apps import NativeApps
from .native_operations.registry_access import registry_failure


def app_definitions(manager: Any) -> list[ToolDefinition]:
    def handler(name: str) -> ToolHandler:
        async def invoke(arguments: dict[str, Any], actor: CallContext) -> Any:
            service = NativeApps(manager, actor, arguments)
            try:
                return await getattr(AppTools(service), name)(**arguments)
            except Exception as error:
                failure = registry_failure(error, {})
                if service.completed and failure.status == "not_started":
                    replacement = service.confirmed_failure(failure)
                    raise replacement from None
                raise failure from error

        return invoke

    definitions = [
        define_tool(
            manager.backend,
            AppTools,
            name,
            handler=handler(name),
            source_module="tools_addons",
            tags=frozenset({"Apps (add-ons)"}),
            annotations=annotations,
            meta={
                "ha_mcp": {
                    "native_support": {
                        "note": "Supervisor is required. Native app management, including opaque GET requests, always requires write admission and is unavailable under hidden-entity enforcement. No retry follows an uncertain app mutation."
                    }
                }
            },
        )
        for name, annotations in (
            ("ha_get_app", read_only_hints("Get Apps (add-ons)", open_world=True)),
            (
                "ha_manage_app",
                write_hints(
                    "Manage App (add-on)",
                    destructive=True,
                    idempotent=False,
                    open_world=True,
                ),
            ),
        )
    ]
    # The factory preserves legacy proxy-GET exemptions by default. Native
    # opaque app requests are always writes, including catalogue filtering.
    return [
        replace(definition, writes=None, operation_timeout=app_operation_timeout)
        if definition.name == "ha_manage_app"
        else definition
        for definition in definitions
    ]
