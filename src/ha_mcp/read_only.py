"""Read-only mode — catalog filtering and call-time write blocking (#1569).

When ``Settings.read_only_mode`` is on (Tools-tab toggle in the web UI,
``read_only_mode`` addon option, or ``READ_ONLY_MODE`` env var), or the
HTTP connection uses the ``/readonly`` endpoint:

- ``ReadOnlyToolsTransform`` hides write-capable tools from the MCP
  catalog at list time, except the exempt mixed read/write tools in
  ``READ_ONLY_EXEMPT_TOOLS``.
- ``ReadOnlyMiddleware`` blocks every write operation at call time with
  a structured ``READ_ONLY_MODE`` error — including the write actions
  of the exempt mixed tools, which stay callable for their read
  operations only.

Both consult the live settings singleton per request, so flipping the
toggle in standalone HTTP mode takes effect without a restart (addon
and stdio modes pick it up on restart, like every other feature flag).
The HTTP endpoint additionally restricts its own request context without
changing the shared settings.

A tool counts as write-capable when its ``readOnlyHint`` annotation is
not ``True`` — the same fail-closed default the policy handlers and the
search-proxy categorizer apply to unannotated tools.

The exempt tools are the mixed read/write tools whose read surface has
no pure-read duplicate elsewhere in the catalog — disabling them
outright would make that data unreachable in read-only mode. Each entry
carries an argument-level predicate that decides, per invocation,
whether the call is a read (allowed) or a write (blocked).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, NoReturn

from ha_mcp._vendor.fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)
from ha_mcp._vendor.fastmcp.server.transforms import Transform
from ha_mcp._vendor.fastmcp.tools import Tool

from .config import get_global_settings
from .errors import ErrorCode, create_error_response
from .renamed_tools import current_tool_name
from .tool_dispatch import CALL_PROXY_META_TOOLS
from .tools.helpers import raise_tool_error

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp.server.transforms import GetToolNext
    from ha_mcp._vendor.fastmcp.utilities.versions import VersionSpec

from .runtime.tool_effects import (
    _ADDON_CONFIG_WRITE_PARAMS,
    READ_ONLY_EXEMPT_TOOLS,
)

logger = logging.getLogger(__name__)

_request_read_only: ContextVar[bool] = ContextVar(
    "ha_mcp_request_read_only", default=False
)


def is_read_only() -> bool:
    """Apply the global setting or the current HTTP endpoint's restriction."""
    return get_global_settings().read_only_mode or _request_read_only.get()


@contextmanager
def read_only_request() -> Iterator[None]:
    """Restrict this request and its nested tool calls without affecting others."""
    token = _request_read_only.set(True)
    try:
        yield
    finally:
        _request_read_only.reset(token)


def read_only_remedy_hint() -> str:
    """User-facing guidance for lifting Read Only Mode.

    Deliberately never names the ``/readonly`` endpoint suffix: #2433 wants
    this restriction to hold even for an untrusted model that could talk a
    user into dropping it. The global setting has no such escape-hatch risk,
    so it stays nameable when it is the (or an) active source.
    """
    if get_global_settings().read_only_mode:
        return (
            "To allow changes, the user must turn off Read Only Mode in the "
            "ha-mcp settings UI (Tools tab) or the app (add-on) configuration."
        )
    return "This connection is read-only. If changes are needed, ask the user."


def is_read_safe(tool: Tool) -> bool:
    """Return True when the tool's annotations declare it read-only."""
    annotations = getattr(tool, "annotations", None)
    return bool(annotations and annotations.read_only_hint is True)


def read_only_visible(tool: Tool) -> bool:
    """Return True when the tool stays in the catalog in read-only mode."""
    return is_read_safe(tool) or tool.name in READ_ONLY_EXEMPT_TOOLS


def _raise_read_only_error(
    name: str, *, blocked_operation: str | None = None, allowed: str | None = None
) -> NoReturn:
    raise_tool_error(
        read_only_error_response(
            name, blocked_operation=blocked_operation, allowed=allowed
        )
    )


def read_only_error_response(
    name: str, *, blocked_operation: str | None = None, allowed: str | None = None
) -> dict[str, Any]:
    """The structured ``READ_ONLY_MODE`` error for a blocked call to *name*."""
    context: dict[str, Any] = {"tool_name": name, "read_only_mode": True}
    if blocked_operation is not None:
        context["blocked_operation"] = blocked_operation
    if blocked_operation is not None and allowed is not None:
        message = (
            f"Read Only Mode is enabled on this Home Assistant MCP server. "
            f"This call to '{name}' is a write operation "
            f"({blocked_operation}) and was blocked — no changes were made. "
            f"While Read Only Mode is on, '{name}' only supports: {allowed}."
        )
    else:
        message = (
            f"Read Only Mode is enabled on this Home Assistant MCP server. "
            f"'{name}' is a write-capable tool, so the call was blocked — "
            f"no changes were made."
        )
    return create_error_response(
        ErrorCode.READ_ONLY_MODE,
        message,
        suggestions=[
            "Continue with read-only tools — searching, getting, and "
            + "listing data all remain available.",
            read_only_remedy_hint(),
        ],
        context=context,
    )


def require_write_access(tool_name: str) -> None:
    """Reject direct tool execution while Read Only Mode is enabled."""
    if is_read_only():
        _raise_read_only_error(tool_name)


class ReadOnlyToolsTransform(Transform):
    """Hide write-capable tools from the catalog while read-only mode is on.

    Installed before the search transforms so the BM25 index never
    indexes hidden write tools. Consults the live flag per request —
    no-op (and no per-call cost beyond the flag check) while it is off.
    """

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        if not is_read_only():
            return tools
        return [t for t in tools if read_only_visible(t)]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        if tool is None or not is_read_only():
            return tool
        return tool if read_only_visible(tool) else None


class ReadOnlyMiddleware(Middleware):
    """Block write operations at call time while read-only mode is on.

    The catalog filter already hides plain write tools, but the
    middleware is the actual enforcement: it covers calls routed through
    the search proxies, the write actions of the exempt mixed tools, and
    direct calls to hidden tools. Annotation lookups go through an
    unfiltered catalog provider (injected by server.py) and are cached
    (rebuilt on a cache miss — see _classify).
    """

    def __init__(self, *, list_tools: Callable[[], Awaitable[Sequence[Tool]]]) -> None:
        self._list_tools = list_tools
        self._read_safe_cache: dict[str, bool] | None = None

    async def _classify(self, name: str) -> str:
        """Classify ``name`` as 'read', 'write', or 'unknown'.

        Backed by the unfiltered catalog; the cache rebuilds on a miss so
        late-registered tools classify correctly. 'unknown' means the
        tool is not registered at all — the call passes through so the
        caller gets the normal unknown-tool error (nothing executable,
        no write risk). An EMPTY catalog is abnormal (broken lookup) and
        classifies everything 'write' — fail closed rather than letting
        calls through unclassified. If the catalog lookup itself RAISES
        we cannot classify anything, so we block the call with the
        structured READ_ONLY_MODE error rather than let the exception
        propagate opaquely (or, worse, let a future try/except return
        'unknown' and silently fail open).
        """
        if self._read_safe_cache is None or name not in self._read_safe_cache:
            try:
                tools = await self._list_tools()
            except Exception:
                logger.exception(
                    "read-only mode: tool catalog lookup failed while "
                    "classifying %s — blocking the call conservatively",
                    name,
                )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.READ_ONLY_MODE,
                        f"Read Only Mode is enabled on this Home Assistant MCP "
                        f"server, and the tool catalog lookup needed to classify "
                        f"'{name}' as read or write failed. The call to '{name}' "
                        f"was blocked conservatively — no changes were made.",
                        suggestions=[
                            "Retry the call — the catalog lookup may succeed "
                            + "on the next attempt.",
                            "If this persists, the MCP server may be "
                            + "misconfigured; check the server logs.",
                        ],
                        context={"tool_name": name, "read_only_mode": True},
                    )
                )
            self._read_safe_cache = {t.name: is_read_safe(t) for t in tools}
        if name in self._read_safe_cache:
            return "read" if self._read_safe_cache[name] else "write"
        if not self._read_safe_cache:
            return "write"
        return "unknown"

    @staticmethod
    def _coerce_arguments(arguments: Any) -> dict[str, Any] | None:
        """Normalise a proxy envelope's ``arguments`` to a dict or None.

        The categorized proxies tolerate ``arguments`` arriving as a JSON
        string (small models sometimes serialize it) and json.loads it
        AFTER this middleware runs (categorized_search.py ~313-352). So:
        a dict is used as-is; an absent value (``None``) becomes ``{}``
        (a legitimate no-argument call, not malformed); a string is
        parsed and only a JSON *object* yields a dict. Anything else — an
        unparseable string, a non-dict JSON value (list/scalar/null), or
        a non-str/non-dict type — returns None, meaning "malformed
        envelope".
        """
        if isinstance(arguments, dict):
            return arguments
        if arguments is None:
            return {}
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except (json.JSONDecodeError, ValueError):
                return None
            return parsed if isinstance(parsed, dict) else None
        return None

    @classmethod
    def _unwrap_proxy_call(
        cls,
        args: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        """Extract the inner (tool, arguments) from a call-proxy envelope.

        The categorized call proxies validate the inner name against
        their category caches BEFORE dispatching — and in read-only mode
        those caches no longer contain the hidden write tools, so a
        proxied write would surface as a generic "tool not found" error
        instead of the explanatory READ_ONLY_MODE one. Unwrapping here
        lets the middleware decide on the inner call first. Mirrors the
        proxy's own double-wrap unwrapping.

        ``arguments`` is coerced at every level (see _coerce_arguments)
        because the proxy accepts it as a JSON string. Returns None when
        there is no usable envelope — including when ``arguments`` is a
        MALFORMED string (json.loads fails, or the JSON is not an object).
        Passing through on a malformed envelope is safe: the proxy
        rejects such ``arguments`` with its own VALIDATION error BEFORE
        the category check and before any dispatch
        (categorized_search.py ~313-352), so no write can occur.
        """
        name = args.get("name")
        arguments = cls._coerce_arguments(args.get("arguments"))
        while (
            isinstance(name, str)
            and name in CALL_PROXY_META_TOOLS
            and isinstance(arguments, dict)
            and isinstance(arguments.get("name"), str)
        ):
            name = arguments.get("name")
            arguments = cls._coerce_arguments(arguments.get("arguments"))
        if not isinstance(name, str):
            return None
        if arguments is None:
            # Malformed inner ``arguments`` (a string that fails
            # json.loads, or a non-object JSON value / non-dict type).
            # Pass through: the proxy raises its own VALIDATION error
            # before any dispatch, so nothing can be written. (An absent
            # ``arguments`` key is NOT malformed — it coerces to ``{}``.)
            return None
        return name, arguments

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        if not is_read_only():
            return await call_next(context)

        # RenamedToolAliasMiddleware runs ahead of this one and normally
        # rewrites a retired name before it gets here. Resolving it again costs
        # a dict lookup and removes the ordering dependency: were this gate ever
        # registered ahead of the alias, it would read a name no exemption is
        # keyed on, which does not fail closed.
        name = current_tool_name(context.message.name)
        args = context.message.arguments or {}

        # Call proxies: decide on the INNER call (see _unwrap_proxy_call).
        # Envelope-less proxy calls pass through because the proxy raises its own
        # validation error for a missing inner name. When the inner call is
        # allowed, proxy dispatch re-enters this middleware with the real tool name
        # anyway (harmless re-check, same verdict). ``ha_search_tools`` is not a
        # dispatch proxy; it reaches generic classification below as an unknown
        # synthetic tool, which passes through to the search transform (or the
        # stale-tool hint path when search is disabled).
        if name in CALL_PROXY_META_TOOLS:
            unwrapped = self._unwrap_proxy_call(args)
            if unwrapped is None:
                return await call_next(context)
            inner_name, inner_args = unwrapped
            # The envelope carries whatever name the client knows the tool by.
            # An exemption is keyed on the current one, and a miss here does
            # not block: _classify would call the stale name unknown, which is
            # not "write", and the call would pass straight through to the
            # proxy — which resolves the rename and dispatches the write tool.
            inner_name = current_tool_name(inner_name)
            exemption = READ_ONLY_EXEMPT_TOOLS.get(inner_name)
            if exemption is not None:
                blocked = exemption.blocked_write(inner_args)
                if blocked is None:
                    return await call_next(context)
                logger.info(
                    "read-only mode blocked proxied write operation of %s (%s)",
                    inner_name,
                    blocked,
                )
                _raise_read_only_error(
                    inner_name, blocked_operation=blocked, allowed=exemption.allowed
                )
            if await self._classify(inner_name) != "write":
                # 'read' is allowed; 'unknown' falls through to the
                # proxy's own not-found error.
                return await call_next(context)
            logger.info(
                "read-only mode blocked proxied call to write tool %s", inner_name
            )
            _raise_read_only_error(inner_name)

        exemption = READ_ONLY_EXEMPT_TOOLS.get(name)
        if exemption is not None:
            blocked = exemption.blocked_write(args)
            if blocked is None:
                return await call_next(context)
            logger.info(
                "read-only mode blocked write operation of %s (%s)", name, blocked
            )
            _raise_read_only_error(
                name, blocked_operation=blocked, allowed=exemption.allowed
            )

        if await self._classify(name) != "write":
            # 'read' is allowed; 'unknown' falls through to FastMCP's
            # normal unknown-tool error.
            return await call_next(context)

        logger.info("read-only mode blocked call to write tool %s", name)
        _raise_read_only_error(name)
        return None  # py/mixed-returns: unreachable, _raise_read_only_error raises


__all__ = [
    "ReadOnlyMiddleware",
    "ReadOnlyToolsTransform",
    "_ADDON_CONFIG_WRITE_PARAMS",
    "is_read_only",
    "is_read_safe",
    "read_only_error_response",
    "read_only_remedy_hint",
    "read_only_request",
    "read_only_visible",
    "require_write_access",
]
