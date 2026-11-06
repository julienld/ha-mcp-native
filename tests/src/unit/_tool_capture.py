"""Capture bound tools for tests that exercise their bodies directly."""

from types import SimpleNamespace
from typing import Any


def capture_tool_registration(target: dict[str, Any]) -> SimpleNamespace:
    def add_tool(function: Any) -> Any:
        target[function.__name__] = function
        return function

    return SimpleNamespace(add_tool=add_tool)
