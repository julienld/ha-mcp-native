"""Exact resource edits and optimistic hashes, independent of transport."""

from typing import Annotated, Any, NoReturn, TypedDict

from pydantic import ConfigDict, Field

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure
from .resource_values import MAX_CONTENT_SIZE, content_sha256

MAX_EDITS = 100


class ResourceEdit(TypedDict):
    """One exact-match replacement in an inline resource."""

    old: Annotated[
        str,
        Field(
            description="Exact text to replace. Must occur exactly once in the "
            "content at the point this edit is applied; include surrounding "
            "context to make it unique."
        ),
    ]
    new: Annotated[str, Field(description="Replacement text (may be empty).")]


ResourceEdit.__pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[attr-defined]


def _count_occurrences(haystack: str, needle: str) -> int:
    """Count occurrences of ``needle``, overlapping ones included.

    ``str.count`` skips overlaps ("aa" in "aaa" is 1), which would let an
    ambiguous ``old`` pass the exactly-once check.
    """
    count = 0
    start = haystack.find(needle)
    while start != -1:
        count += 1
        start = haystack.find(needle, start + 1)
    return count


def _raise_edit_error(
    reason: str, message: str, context: dict[str, Any], suggestions: list[str]
) -> NoReturn:
    raise_failure(
        create_error_response(
            code=ErrorCode.VALIDATION_INVALID_PARAMETER,
            message=f"{message}. Nothing was saved.",
            context={"reason": reason, **context},
            suggestions=suggestions,
        )
    )


def apply_resource_edits(content: str, edits: list[ResourceEdit]) -> str:
    """Apply ``edits`` in order to a copy of ``content`` and return the result.

    Raises a structured ``ToolError`` (``reason`` in its context) on the first
    edit that cannot apply, so the caller never sees a partial result.
    """
    invalid = [
        index
        for index, edit in enumerate(edits)
        if not edit["old"] or edit["old"] == edit["new"]
    ]
    if invalid:
        _raise_edit_error(
            "invalid_edit",
            f"Edit(s) at index {', '.join(map(str, invalid))} have an empty "
            "'old' or an 'old' identical to 'new'",
            {"edit_index": invalid[0], "invalid_edit_indexes": invalid},
            ["Every edit needs a non-empty 'old' that differs from its 'new'"],
        )
    # Bound the work before the loop: each edit scans and copies the whole
    # intermediate text, and the result's own size check only runs after it.
    added = 0
    for index, edit in enumerate(edits):
        try:
            added += len(edit["new"].encode("utf-8"))
        except UnicodeEncodeError:
            _raise_edit_error(
                "invalid_edit",
                f"Edit {index}: 'new' contains an unpaired surrogate, which "
                "is not valid UTF-8",
                {"edit_index": index, "invalid_edit_indexes": [index]},
                ["Send 'new' as valid Unicode text"],
            )
    if added > MAX_CONTENT_SIZE:
        _raise_edit_error(
            "edits_too_large",
            f"The edits' 'new' texts total {added:,} bytes (max {MAX_CONTENT_SIZE:,})",
            {"size": added, "limit": MAX_CONTENT_SIZE},
            ["Replace most of a resource with content= instead of edits"],
        )

    result = content
    for index, edit in enumerate(edits):
        matches = _count_occurrences(result, edit["old"])
        if matches == 0:
            _raise_edit_error(
                "old_not_found",
                f"Edit {index}: 'old' text not found in the resource",
                {"edit_index": index},
                [
                    "Match the stored text exactly, including whitespace and quotes",
                    "Earlier edits apply first; account for text they changed",
                    "Fetch the current code with ha_config_list_dashboard_resources(include_content=True)",
                ],
            )
        if matches > 1:
            _raise_edit_error(
                "old_not_unique",
                f"Edit {index}: 'old' text occurs {matches} times in the resource",
                {"edit_index": index, "matches": matches},
                ["Extend 'old' with surrounding context so it matches exactly once"],
            )
        result = result.replace(edit["old"], edit["new"], 1)
    return result


def check_config_hash(content: str, config_hash: str | None) -> None:
    """Refuse the patch when the stored content no longer has the caller's hash."""
    if config_hash is None:
        return
    current = content_sha256(content)
    if config_hash.strip().lower() != current:
        _raise_edit_error(
            "hash_mismatch",
            "The stored resource does not match config_hash; it changed "
            "since you last read it",
            {"current_config_hash": current},
            [
                "Re-read it with ha_config_list_dashboard_resources(include_hash=True, include_content=True)",
                "Rebuild the edits against the current content",
            ],
        )
