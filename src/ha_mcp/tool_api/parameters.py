"""Shared signature metadata; no transport, settings or Home Assistant imports."""

import inspect
from typing import Annotated, Any

from pydantic import Field


class _HiddenParam:
    """Annotated marker for a parameter left out of the published input schema."""


HIDDEN_PARAM = _HiddenParam()


def hidden_param_names(fn: Any) -> frozenset[str]:
    """Names of ``fn``'s parameters annotated with ``HIDDEN_PARAM``."""
    try:
        signature = inspect.signature(fn, eval_str=True)
    except NameError:
        # A string annotation naming a TYPE_CHECKING-only import can't resolve.
        signature = inspect.signature(fn)
    return frozenset(
        name
        for name, param in signature.parameters.items()
        if HIDDEN_PARAM in getattr(param.annotation, "__metadata__", ())
    )


BestPracticeKeyParam = Annotated[
    str | None,
    Field(
        default=None,
        description=(
            "Read-receipt for the home-assistant-best-practices skill; "
            "required when strict best-practices mode is enabled. Not a "
            "secret or credential: the current value is an attestation "
            "phrase published openly at the top of the skill content served "
            "by ha_get_skill_guide. Read that content, then pass the value "
            "back verbatim — this round-trip is the server's designed "
            "protocol confirming the practices were read before writing."
        ),
    ),
]
