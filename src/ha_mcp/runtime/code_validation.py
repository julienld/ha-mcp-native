"""Shared sandbox input validation and error classification."""

import re
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure

SAVE_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _classify_sandbox_error(exc: Exception) -> tuple[ErrorCode, str, list[str]]:
    """Map a sandbox exception to ``(code, message, suggestions)``.

    Monty wraps inner exceptions in ``MontyRuntimeError`` with the inner
    type name embedded in the string representation, so we inspect both
    ``type(exc).__name__`` and ``str(exc)`` and pick the most specific
    bucket. Three categories:

    * ``SANDBOX_LIMIT_EXCEEDED`` — memory / time / recursion / invocation
      limits the sandbox runtime enforces.
    * ``SANDBOX_SYNTAX_UNSUPPORTED`` — features Monty doesn't implement
      (imports, classes, ``with``, ``match``, etc.) or hard syntax errors.
    * ``SANDBOX_RUNTIME_ERROR`` — anything else; opaque runtime failure
      whose root cause is in the LLM-authored code.

    The suggestions are tailored per category so the LLM can self-recover
    instead of seeing every failure as "check the Python code for syntax
    errors" (which was the prior behaviour and actively misled callers
    when the real cause was a memory cap or a missing module import).
    """
    exc_text = str(exc)
    exc_type = type(exc).__name__
    short = exc_text[:200]

    def _matches(*needles: str) -> bool:
        return any(needle in exc_type or needle in exc_text for needle in needles)

    if _matches("MemoryError"):
        return (
            ErrorCode.SANDBOX_LIMIT_EXCEEDED,
            f"Sandbox memory limit exceeded: {short}",
            [
                "Reduce memory usage in your code (smaller intermediate "
                + "data structures, no large list accumulation).",
                "Stream results via call_tool calls rather than building "
                + "them up in-process.",
                "Operator can raise CODE_MODE_MAX_MEMORY (max 256 MB).",
            ],
        )
    if _matches("RecursionError"):
        return (
            ErrorCode.SANDBOX_LIMIT_EXCEEDED,
            f"Sandbox recursion limit exceeded: {short}",
            [
                "Convert deep recursion to iteration (while/for with an "
                + "explicit stack).",
                "Operator can raise CODE_MODE_MAX_RECURSION (max 10000).",
            ],
        )
    if _matches("TimeoutError", "timed out", "wall-clock", "max_duration"):
        return (
            ErrorCode.SANDBOX_LIMIT_EXCEEDED,
            f"Sandbox time limit exceeded: {short}",
            [
                "Optimise the code to finish within the wall-clock limit.",
                "Break the work into smaller chunks across multiple calls.",
                "Operator can raise CODE_MODE_MAX_DURATION (max 300s).",
            ],
        )

    if _matches("ModuleNotFoundError", "No module named"):
        return (
            ErrorCode.SANDBOX_SYNTAX_UNSUPPORTED,
            f"Imports are not allowed in the sandbox: {short}",
            [
                "Remove all 'import' / 'from ... import' statements.",
                "Use the injected helpers instead: api_get, api_post, "
                + "ws_send, call_tool, delete_saved_tool.",
            ],
        )
    if _matches("NotImplementedError", "does not yet support", "context manager"):
        return (
            ErrorCode.SANDBOX_SYNTAX_UNSUPPORTED,
            f"Sandbox does not support this Python feature: {short}",
            [
                "Monty's sandboxed interpreter is a Python subset — "
                + "context managers (with), match statements, class "
                + "definitions, and some builtins are unavailable.",
                "Rewrite using basic statements and the injected helpers.",
            ],
        )
    if _matches("SyntaxError"):
        return (
            ErrorCode.SANDBOX_SYNTAX_UNSUPPORTED,
            f"Code did not parse: {short}",
            [
                "Fix the syntax error and retry.",
                "Reminder: no class definitions, no imports, no 'with' "
                + "or 'match' statements.",
            ],
        )

    return (
        ErrorCode.SANDBOX_RUNTIME_ERROR,
        f"Sandbox runtime error: {short}",
        [
            f"Exception type: {exc_type}",
            "Some Python builtins behave differently in Monty (e.g. "
            + "next() requires an iterator, not a list).",
            "Check the values you're passing to api_get/api_post/ws_send/call_tool.",
            "Use 'await' before any call to api_get/api_post/ws_send/call_tool.",
        ],
    )


def _validate_custom_tool_modes(
    code: str | None, run_saved: str | None, list_saved: bool
) -> None:
    """Raise if more than one of code/run_saved/list_saved was provided.

    ``code`` and ``run_saved`` are mutually exclusive (either run new
    code or re-run a saved tool, not both). ``list_saved`` is also
    exclusive — it inspects state and must not coexist with execution.
    ``save_as`` and ``justification`` are modifiers for the ``code``
    mode and don't count as a "mode" on their own.
    """
    modes_active = sum(
        1
        for v in (
            bool(code and code.strip()),
            bool(run_saved is not None),
            bool(list_saved),
        )
        if v
    )
    if modes_active > 1:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "code, run_saved, and list_saved are mutually exclusive — "
                "specify exactly one.",
                suggestions=[
                    "ha_manage_custom_tool(code='...', justification='...')",
                    "ha_manage_custom_tool(run_saved='tool_name')",
                    "ha_manage_custom_tool(list_saved=True)",
                ],
            )
        )


def _validate_custom_tool_code_inputs(
    code: str | None, justification: str | None, save_as: str | None
) -> tuple[str, str]:
    """Validate the code/justification/save_as inputs for execute-code mode.

    Raises ToolError for missing code, missing justification, or an
    invalid save_as name. Returns the narrowed ``(code, justification)``
    pair on success.
    """
    if not code or not code.strip():
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Provide code to execute, run_saved to reuse a saved tool, "
                "or list_saved=True to list saved tools",
                suggestions=[
                    "ha_manage_custom_tool(code='...', justification='...')",
                    "ha_manage_custom_tool(run_saved='tool_name')",
                    "ha_manage_custom_tool(list_saved=True)",
                ],
            )
        )

    if not justification or not justification.strip():
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "justification is required when providing code",
                suggestions=["Explain why no existing tool can accomplish this task"],
            )
        )

    if save_as is not None and not SAVE_NAME_PATTERN.match(save_as):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid save_as name: '{save_as}'. "
                "Use alphanumeric characters and underscores, 1-64 chars.",
                suggestions=["Example: save_as='movie_mode'"],
            )
        )

    return code, justification


def _sandbox_error(code: ErrorCode, message: str) -> dict[str, Any]:
    """Build an error dict to return to sandbox code (not a tool-level error).

    These are returned to the sandbox caller, not to the MCP client,
    so they intentionally do NOT use raise_tool_error.
    """
    err: dict[str, Any] = {"error": {"code": str(code), "message": message}}
    err["success"] = False
    return err
