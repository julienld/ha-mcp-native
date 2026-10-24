"""Native crash dump presentation over the same bounded file window."""

from typing import Any, Literal

from .log_common import SUPERVISOR_SEARCH_WINDOW_LINES

FAULT_LOG_PATH = "home-assistant.log.fault"


MIN_COMPONENT_VERSION_FAULT_LOG = "2.2.0"


FAULT_LOG_WINDOW_LINES = SUPERVISOR_SEARCH_WINDOW_LINES


_FATAL_MARKER = "Fatal Python error:"


_NO_CRASH_MESSAGE = (
    "No native crash recorded: home-assistant.log.fault is empty. Ordinary "
    "Python errors never land here; use source='system' or source='error_log' "
    "for those."
)


def _is_missing_file_error(error: str) -> bool:
    return "does not exist" in error or "not a file" in error


def _no_crash(data: dict[str, Any], total_lines: int) -> dict[str, Any]:
    data.update(
        crash_recorded=False,
        log="",
        total_lines=total_lines,
        returned_lines=0,
        has_more=False,
        message=_NO_CRASH_MESSAGE,
    )
    return data


def _split_blocks(lines: list[str]) -> list[list[str]]:
    """Group the window into crash blocks, each opening with the fatal marker.

    Lines before the first marker are the tail of a block whose header fell
    outside the window; they form a leading partial block so nothing is lost.
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith(_FATAL_MARKER) and current:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _shape_crash_page(
    data: dict[str, Any],
    lines: list[str],
    *,
    limit: int,
    offset: int,
    search: str | None,
    order: Literal["newest", "oldest"],
) -> dict[str, Any]:
    """Order whole crash blocks, then page through the flattened text.

    ``order='newest'`` puts the latest block first; every block keeps its own
    line order so the traceback stays readable. ``search`` selects whole
    blocks (any line matching) rather than lines, so a hit inside a traceback
    still comes back with its ``Fatal Python error`` header and the rest of
    the frames. ``offset``/``limit`` slice the assembled text from its start,
    and ``has_more``/``next_offset`` continue it.
    """
    blocks = _split_blocks(lines)
    fatal_blocks = sum(1 for block in blocks if block[0].startswith(_FATAL_MARKER))
    if order == "newest":
        blocks.reverse()

    filters_applied: dict[str, str] = {}
    if search:
        search_lower = search.lower()
        blocks = [b for b in blocks if any(search_lower in ln.lower() for ln in b)]
        filters_applied["search"] = search
    matched_blocks = len(blocks)
    flat = [line for block in blocks for line in block]

    page = flat[offset : offset + limit]
    has_more = offset + limit < len(flat)
    data.update(
        crash_recorded=True,
        log="\n".join(page),
        returned_lines=len(page),
        fatal_error_blocks_in_window=fatal_blocks,
        has_more=has_more,
    )
    if has_more:
        data["next_offset"] = offset + limit
        # Carry the active filter: the next page must slice the same
        # filtered block list, or offset lands in unrelated blocks.
        search_arg = f", search={search!r}" if search else ""
        data["pagination_hint"] = (
            f"ha_get_logs(source='fault_log', offset={offset + limit}, "
            f"limit={limit}, order='{order}'{search_arg})"
        )
    if filters_applied:
        data["filters_applied"] = filters_applied
        data["matched_blocks"] = matched_blocks
    return data
