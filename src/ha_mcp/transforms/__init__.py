"""Custom FastMCP transforms for ha-mcp."""

from typing import TYPE_CHECKING, Any

from ha_mcp._lazy_exports import resolve_export

from .categorized_search import (
    DEFAULT_PINNED_TOOLS,
    Capability,
    CategorizedSearchTransform,
    SearchKeywordsTransform,
    categorize_capability,
)
from .lite_docstrings import LiteDocstringsTransform
from .write_tool_note import DESKTOP_APPROVAL_NOTE, WriteToolNoteTransform

if TYPE_CHECKING:
    from .component_search import ComponentSearchSchemaTransform

_LAZY_EXPORTS = {
    "ComponentSearchSchemaTransform": (
        ".component_search",
        "ComponentSearchSchemaTransform",
    )
}


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _LAZY_EXPORTS, name)


__all__ = [
    "Capability",
    "CategorizedSearchTransform",
    "ComponentSearchSchemaTransform",
    "DEFAULT_PINNED_TOOLS",
    "DESKTOP_APPROVAL_NOTE",
    "LiteDocstringsTransform",
    "SearchKeywordsTransform",
    "WriteToolNoteTransform",
    "categorize_capability",
]
