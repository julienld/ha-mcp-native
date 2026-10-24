"""Recheck the current invocation's live policy at semantic write dispatch."""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

from ha_mcp.tool_api import CallContext, ToolFailure


@dataclass(frozen=True)
class MutationAdmission:
    context: CallContext
    writes: bool
    check_policy: Callable[[], None]


current_admission: ContextVar[MutationAdmission | None] = ContextVar(
    "ha_mcp_mutation_admission", default=None
)


def check_mutation_admission(context: CallContext) -> None:
    admission = current_admission.get()
    if admission is None:
        return
    if context != admission.context:
        raise PermissionError("A tool cannot change the admitted actor for a mutation")
    if not admission.writes:
        raise ToolFailure(
            "READ_ONLY",
            "A read-only tool cannot dispatch a mutation.",
            status="not_started",
        )
    admission.check_policy()
