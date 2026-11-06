"""Run Monty with explicit external capabilities, without a transport context."""

import asyncio
from typing import Any

from ha_mcp.utils.owned_executor import await_owned_io


async def run_sandboxed_code(
    code: str,
    external_functions: dict[str, Any],
    settings: Any,
    Monty: Any,
    ResourceLimits: Any,
) -> Any:
    """Execute code in the pydantic-monty sandbox.

    External functions available to sandbox code:
    - api_get(endpoint) — GET request to HA REST API
    - api_post(endpoint, data) — POST request to HA REST API
    - ws_send(message) — send a HA WebSocket command and return its result
    - call_tool(name, args) — call a registered MCP tool (for existing tools)

    See ``_SandboxBridge`` for the error shape contract these external
    functions share.
    """
    m = await await_owned_io(
        asyncio.to_thread(Monty, code, script_name="ha_manage_custom_tool.py")
    )
    run_kwargs: dict[str, Any] = {
        "external_functions": external_functions,
        "limits": ResourceLimits(
            max_duration_secs=settings.code_mode_max_duration,
            max_memory=settings.code_mode_max_memory,
            max_recursion_depth=settings.code_mode_max_recursion,
        ),
    }

    # Monty.run_async() is the preferred path but may not be available on
    # all platforms (e.g., ARM wheels).  Fall back to the deprecated
    # module-level run_monty_async.
    if hasattr(m, "run_async"):
        return await m.run_async(**run_kwargs)

    # Import in its own try so an ImportError raised by the body of
    # run_monty_async (e.g. a missing native shim) propagates instead of
    # being misattributed to "module-level run_monty_async not found".
    try:
        from pydantic_monty import run_monty_async
    except ImportError:
        run_monty_async = None  # type: ignore[assignment]
    if run_monty_async is not None:
        return await run_monty_async(m, **run_kwargs)

    # No async execution path available — fail explicitly rather than
    # silently breaking call_tool with a sync fallback.
    raise RuntimeError(
        "pydantic-monty async execution is not available on this platform. "
        "ha_manage_custom_tool requires Monty.run_async() or run_monty_async()."
    )
