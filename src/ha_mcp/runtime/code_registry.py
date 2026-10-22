"""Saved-script mutation semantics with an explicit registry and persistence port."""

import logging
from collections.abc import Callable
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .code_persistence import MAX_SAVED_TOOLS
from .code_validation import SAVE_NAME_PATTERN
from .errors import raise_failure

logger = logging.getLogger(__name__)


class SavedToolsRegistry:
    def __init__(
        self,
        tools: dict[str, dict[str, str]],
        persist: Callable[[str, dict[str, dict[str, str]]], bool],
    ) -> None:
        self.tools = tools
        self.persist = persist

    def list(self) -> dict[str, Any]:
        """Build the ``list_saved=True`` response payload.

        The saved-tools dict is nested under a stable ``saved_tools`` key
        rather than spread directly under ``data`` because the name pattern
        (``^[a-zA-Z_][a-zA-Z0-9_]{0,63}$``) accepts values like ``result``,
        ``count``, ``code`` — every one of which is also a key the *other*
        response shapes use. A consumer reading ``r["data"]["result"]`` after
        a list_saved call would otherwise get a saved-tool entry instead of a
        run-result.
        """
        return {
            "success": True,
            "data": {
                "saved_tools": {
                    name: {
                        "code": info["code"],
                        "justification": info["justification"],
                    }
                    for name, info in self.tools.items()
                },
                "count": len(self.tools),
            },
        }

    def save_as(
        self,
        response: dict[str, Any],
        save_as: str,
        code: str,
        justification: str,
        path: str,
    ) -> None:
        """Save the just-executed code under ``save_as``, updating ``response["data"]``.

        Mutates ``response["data"]`` in place: sets ``saved_as`` on success,
        or rolls back the in-memory write and sets ``saved_as=None`` plus a
        ``save_warning`` on persistence failure. Raises ToolError if the
        saved-tools cache is full.
        """
        if save_as not in self.tools and len(self.tools) >= MAX_SAVED_TOOLS:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    f"Saved-tools cache is full ({MAX_SAVED_TOOLS} entries). "
                    "Delete a tool with delete_saved_tool(name) before "
                    "saving a new one.",
                    suggestions=[
                        "Use list_saved=True to see existing saved tools",
                        "Use code='delete_saved_tool(\"<name>\")' to remove one",
                    ],
                )
            )
        previous = self.tools.get(save_as)
        self.tools[save_as] = {
            "code": code,
            "justification": justification,
        }
        response["data"]["saved_as"] = save_as
        logger.info("Saved custom tool as '%s'", save_as)
        persisted = self.persist(path, self.tools)
        if not persisted:
            # Roll back the in-memory write so the cache matches
            # what's on disk (or, on next restart, what's loaded).
            # Surface a warning in the response so the LLM knows
            # the save_as didn't actually durable, while still
            # returning success=True for the code execution itself.
            if previous is None:
                self.tools.pop(save_as, None)
            else:
                self.tools[save_as] = previous
            response["data"]["saved_as"] = None
            response["data"]["save_warning"] = (
                f"save_as={save_as!r} was attempted but the persistence "
                "write failed; the entry was rolled back from the "
                "in-memory cache. Check operator logs for the "
                "underlying I/O error."
            )
        elif not path:
            # Blank path = persistence disabled: the in-memory save
            # succeeded but is lost on restart. Surface a warning so
            # the agent doesn't assume the entry is durable — mirrors
            # the write-failure branch above.
            response["data"]["save_warning"] = (
                f"save_as={save_as!r} is kept in memory only — "
                "code_mode_saved_tools_path is unset, so custom tools "
                "are not persisted and are lost on restart. Set a path "
                "on persistent storage to keep them."
            )

    def delete(self, name: Any, path: str) -> dict[str, Any]:
        """Remove a previously saved custom tool by name.

        Sandbox helper. Returns ``{"deleted": True, "name": name}`` on
        success, ``{"error": "..."}`` on validation failure, missing
        name, or persistence failure. When persistence is configured
        and the on-disk write fails, the in-memory deletion is rolled
        back so the next save_as / list_saved doesn't show a different
        view than the next process restart.
        """
        if not isinstance(name, str):
            return {"error": "delete_saved_tool(name) requires a string name"}
        if not SAVE_NAME_PATTERN.match(name):
            return {
                "error": (
                    f"Invalid saved-tool name {name!r}. "
                    "Use alphanumeric characters and underscores, 1-64 chars."
                )
            }
        if name not in self.tools:
            return {"error": f"No saved tool named {name!r}"}
        # Snapshot the entry before deleting so we can restore it on
        # persist failure (otherwise the in-memory cache and disk would
        # disagree, and the on-restart hydration would resurrect the
        # entry the LLM already saw "deleted").
        previous = self.tools[name]
        del self.tools[name]
        if not self.persist(path, self.tools):
            self.tools[name] = previous
            return {
                "error": (
                    f"Deleted {name!r} from in-memory cache but the "
                    "persistence write failed; rolled back. Check "
                    "operator logs for the underlying I/O error."
                )
            }
        logger.info("Deleted saved custom tool '%s'", name)
        return {"deleted": True, "name": name}
