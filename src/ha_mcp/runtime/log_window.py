"""Detached log window values shared by native and remote readers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorLogPage:
    """A bounded window and the source's verdict about older history.

    first_cursor is Supervisor's identity for the oldest fetched entry. File
    readers leave it unset and use integer line offsets instead.
    """

    text: str
    has_more: bool
    first_cursor: str | None = None
    pagination_complete: bool = True


# Supervisor floors newest-anchored windows to two entries: :0:1 starts at
# the oldest entry, while :-1:2 starts at the newest pair.
MIN_LOG_WINDOW_LINES = 2
