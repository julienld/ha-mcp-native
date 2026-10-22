"""One custom-tool workflow and public signature for native and remote facades."""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import ToolFailure

from .code_validation import (
    _classify_sandbox_error,
    _validate_custom_tool_code_inputs,
    _validate_custom_tool_modes,
)
from .errors import raise_failure

logger = logging.getLogger(__name__)


class CodeSession:
    def __init__(
        self,
        *,
        list_saved: Callable[[], Awaitable[dict[str, Any]]],
        get_saved: Callable[[str], Awaitable[dict[str, str] | None]],
        save_tool: Callable[[dict[str, Any], str, str, str], Awaitable[None]],
        run_code: Callable[[str], Awaitable[Any]],
        passthrough_errors: tuple[type[Exception], ...] = (ToolFailure,),
    ) -> None:
        self._list_saved = list_saved
        self._get_saved = get_saved
        self._save_tool = save_tool
        self._run_code = run_code
        self._passthrough_errors = passthrough_errors

    async def ha_manage_custom_tool(
        self,
        code: Annotated[
            str | None,
            Field(
                description="Python code to execute.  Last expression is the return value."
            ),
        ] = None,
        justification: Annotated[
            str | None,
            Field(description="Why no existing tool works."),
        ] = None,
        save_as: Annotated[
            str | None,
            Field(
                description=(
                    "Save the tool under this name for reuse "
                    "(alphanumeric/underscores, max 64 chars)."
                )
            ),
        ] = None,
        run_saved: Annotated[
            str | None,
            Field(description="Name of a previously saved tool to re-run."),
        ] = None,
        list_saved: Annotated[
            bool, Field(description="Set True to list all saved tools.")
        ] = False,
    ) -> dict[str, Any]:
        """Create and run a custom tool in a sandbox, or manage saved custom tools.

        LAST RESORT — search for existing tools first.

        **Modes** (mutually exclusive): ``code`` + ``justification``;
        ``run_saved``; ``list_saved=True``.

        **Available functions in sandbox:**
        - ``api_get(endpoint)`` / ``api_post(endpoint, data)`` — HA REST API,
          for operations not covered by existing tools
        - ``ws_send(message)`` — send a HA WebSocket command, for operations
          only available over WebSocket (most registry CRUD, template rendering,
          Lovelace operations). ``message`` must include a ``"type"`` field; the
          MCP server adds ``id`` and handles auth.
        - ``call_tool(name, args)`` — call a registered MCP tool
        - ``delete_saved_tool(name)`` — remove a previously saved custom tool.
          Returns ``{"deleted": True, "name": name}`` or ``{"error": ...}``.

        Saved tools persist across server restarts when
        ``CODE_MODE_SAVED_TOOLS_PATH`` is set (the app sets this by default to
        ``/data/saved_tools.json``).

        Example — list areas via WebSocket:
        ```python
        result = await ws_send({"type": "config/area_registry/list"})
        result.get("result", [])
        ```

        Example — chain existing tools:
        ```python
        result = await call_tool("ha_search", {"query": "light", "limit": 5})
        lights = result.get("entities", [])
        for e in lights:
            await call_tool("ha_call_service", {"domain": "light", "service": "turn_off", "entity_id": e["entity_id"]})
        {"turned_off": len(lights)}
        ```
        """
        _validate_custom_tool_modes(code, run_saved, list_saved)

        # --- Mode: list saved tools ---
        if list_saved:
            return await self._list_saved()

        # --- Mode: run saved tool ---
        if run_saved is not None:
            return await self.run_saved(run_saved)

        # --- Mode: execute code ---
        code, justification = _validate_custom_tool_code_inputs(
            code, justification, save_as
        )

        # ``%r`` (repr) defends against log-line injection by escaping
        # ``\r`` / ``\n`` / ``\t`` as literal ``\r``/``\n``/``\t``
        # sequences in the formatted output — same primitive used by
        # the audit-log endpoint/type fields below. Truncate to keep
        # log lines bounded; an LLM can supply a multi-KB
        # justification.
        logger.info(
            "ha_manage_custom_tool invoked — justification: %r",
            justification[:200],
        )
        # Code is logged at DEBUG and the multi-line shape is intentional
        # (the LLM-authored snippet is the operator's primary forensic
        # artefact). No control-char sanitisation here because the log
        # format string already opens a fresh line — there's nothing to
        # inject into.
        logger.debug("ha_manage_custom_tool code:\n%s", code)

        result = await self.execute_code(code, justification)

        response: dict[str, Any] = {
            "success": True,
            "data": {"result": result, "justification": justification},
        }

        if save_as:
            await self._save_tool(response, save_as, code, justification)

        return response

    async def run_saved(self, run_saved: str) -> dict[str, Any]:
        """Re-run a previously saved custom tool by name.

        Raises ToolFailure (RESOURCE_NOT_FOUND) if ``run_saved`` isn't in the
        saved-tools cache, or a classified sandbox error if execution fails.
        """
        saved = await self._get_saved(run_saved)
        if saved is None:
            raise_failure(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"No saved tool named '{run_saved}'",
                    suggestions=[
                        "Use ha_manage_custom_tool(list_saved=True) to see saved tools",
                        "Use ha_manage_custom_tool(code=...) to create a new tool",
                    ],
                    context={"tool_name": run_saved},
                )
            )

        logger.info("Running saved tool '%s'", run_saved)

        try:
            result = await self._run_code(saved["code"])
        except self._passthrough_errors:
            raise
        except Exception as e:  # noqa: BLE001
            code, message, suggestions = _classify_sandbox_error(e)
            raise_failure(
                create_error_response(
                    code,
                    message,
                    suggestions=[
                        "The saved code may no longer work in the "
                        + "current sandbox or HA configuration.",
                        "Use ha_manage_custom_tool(code=...) to "
                        + "create an updated version.",
                        *suggestions,
                    ],
                    context={
                        "sandbox_error_type": type(e).__name__,
                        "saved_tool_name": run_saved,
                    },
                )
            )

        return {
            "success": True,
            "data": {"result": result, "saved_tool": run_saved},
        }

    async def execute_code(self, code: str, justification: str) -> Any:
        """Run freshly-provided sandbox code, classifying failures for the caller."""
        try:
            return await self._run_code(code)
        except self._passthrough_errors:
            raise
        except Exception as e:  # noqa: BLE001
            err_code, err_message, err_suggestions = _classify_sandbox_error(e)
            raise_failure(
                create_error_response(
                    err_code,
                    err_message,
                    suggestions=err_suggestions,
                    context={
                        "sandbox_error_type": type(e).__name__,
                        "justification": justification[:200],
                    },
                )
            )
