"""Small transport-neutral tool contract. No Home Assistant or MCP imports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CallContext:
    """Identity supplied by a trusted entry point, revalidated at every call."""

    user_id: str | None
    client_id: str = "mcp"
    language: str = "en"
    credential: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    read_only: bool = False
    allowed_tools: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Public data remains unchanged; execution metadata travels separately."""

    data: Any
    is_error: bool = False
    status: str | None = None
    operation_id: str | None = None


class ToolFailure(Exception):
    """A classified failure whose payload can cross either tool facade."""

    def __init__(
        self, code: str, message: str, *, data: Any = None, status: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.data = (
            data
            if data is not None
            else {"success": False, "error": {"code": code, "message": message}}
        )


class UnsupportedOperation(ToolFailure):
    """An explicit backend capability miss; never a retry instruction."""

    def __init__(self, operation: str) -> None:
        super().__init__(
            "UNSUPPORTED_OPERATION",
            f"This backend does not implement operation {operation!r}.",
            status="not_started",
        )


ToolHandler = Callable[[dict[str, Any], CallContext], Awaitable[Any]]
ArgumentValidator = Callable[[dict[str, Any]], dict[str, Any]]
PolicyGuard = Callable[
    [str, dict[str, Any], CallContext, bool], Awaitable[ToolResult | None]
]
ResultFilter = Callable[
    [str, dict[str, Any], CallContext, ToolResult], Awaitable[ToolResult]
]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler = field(repr=False, compare=False)
    annotations: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    validate: ArgumentValidator | None = field(default=None, repr=False, compare=False)
    tags: frozenset[str] = frozenset()
    source_module: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        return self.annotations.get("readOnlyHint") is True


class Backend(Protocol):
    async def authorize(self, context: CallContext) -> None:
        """Check the live actor, not only a previously constructed HA Context."""

    async def execute(
        self, operation: str, arguments: dict[str, Any], context: CallContext
    ) -> Any:
        """Invoke a supported semantic operation, without transport fallback."""


class BackendTools:
    """Bind a domain's shared tool methods to one backend and validated actor."""

    def __init__(self, backend: Backend, context: CallContext) -> None:
        self.backend = backend
        self.context = context


__all__ = [
    "Backend",
    "BackendTools",
    "CallContext",
    "PolicyGuard",
    "ResultFilter",
    "ToolDefinition",
    "ToolFailure",
    "ToolResult",
    "UnsupportedOperation",
]
