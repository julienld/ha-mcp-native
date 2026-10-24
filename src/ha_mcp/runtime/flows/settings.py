"""Invocation-local formatting options, never credentials or actor authority."""

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ha_mcp.errors import HomeAssistantConnectionError
from ha_mcp.tool_api import Backend, CallContext


@dataclass(frozen=True, slots=True)
class FlowSettings:
    redact: Callable[[], bool]
    no_answer: tuple[type[Exception], ...] = (HomeAssistantConnectionError, OSError)


_DEFAULT_SETTINGS = FlowSettings(lambda: False)
_settings: ContextVar[FlowSettings] = ContextVar(
    "ha_mcp_flow_settings", default=_DEFAULT_SETTINGS
)


def redaction_enabled() -> bool:
    return _settings.get().redact()


def no_answer_errors() -> tuple[type[Exception], ...]:
    return _settings.get().no_answer


@contextmanager
def bind_flow_settings(settings: FlowSettings) -> Iterator[None]:
    token = _settings.set(settings)
    try:
        yield
    finally:
        _settings.reset(token)


@asynccontextmanager
async def backend_flow_settings(
    backend: Backend, context: CallContext
) -> AsyncIterator[None]:
    flags = await backend.execute("config.flow.settings", {}, context)
    with bind_flow_settings(
        FlowSettings(lambda: flags["redact_secrets"], no_answer_errors())
    ):
        yield
