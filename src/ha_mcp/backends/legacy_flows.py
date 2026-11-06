"""Legacy exception/formatting facade over the shared config-flow algorithms."""

import functools
import inspect
from collections.abc import Callable
from typing import Any

from ha_mcp.runtime.flows.settings import FlowSettings, bind_flow_settings
from ha_mcp.tool_api import ToolFailure


def _settings() -> FlowSettings:
    from ha_mcp.client.rest_client import _NO_ANSWER_ERRORS
    from ha_mcp.redaction import redaction_enabled

    return FlowSettings(redaction_enabled, _NO_ANSWER_ERRORS)


def legacy_flow(function: Callable[..., Any]) -> Callable[..., Any]:
    """Preserve callers' ToolError while the implementation raises ToolFailure."""
    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def asynchronous(*args: Any, **kwargs: Any) -> Any:
            from ha_mcp.utils.tool_errors import raise_tool_error

            with bind_flow_settings(_settings()):
                try:
                    return await function(*args, **kwargs)
                except ToolFailure as exc:
                    raise_tool_error(exc.data)

        return asynchronous

    @functools.wraps(function)
    def synchronous(*args: Any, **kwargs: Any) -> Any:
        from ha_mcp.utils.tool_errors import raise_tool_error

        with bind_flow_settings(_settings()):
            try:
                return function(*args, **kwargs)
            except ToolFailure as exc:
                raise_tool_error(exc.data)

    return synchronous


def legacy_helper_flow(function: Callable[..., Any]) -> Callable[..., Any]:
    """Bind only the explicit client parameter; keep all legacy call signatures."""
    signature = inspect.signature(function)
    if "client" not in signature.parameters:
        return legacy_flow(function)

    @functools.wraps(function)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        from ha_mcp.runtime.flows.session import FlowSession
        from ha_mcp.tool_api import CallContext

        from .remote import RemoteBackend

        bound = signature.bind(*args, **kwargs)
        client = bound.arguments["client"]
        if not isinstance(client, FlowSession):
            bound.arguments["client"] = FlowSession(
                RemoteBackend(client), CallContext(None)
            )
        return await function(*bound.args, **bound.kwargs)

    return legacy_flow(invoke)
