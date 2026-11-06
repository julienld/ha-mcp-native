"""Regressions at the upstream record/native shared-contract boundary."""

import json
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.error_log_journald import JournaldPage, journald_page
from ha_mcp.runtime.error_log_parsing import _split_log_records
from ha_mcp.runtime.log_query import _validate_log_offset
from ha_mcp.tool_api import ToolFailure
from ha_mcp.tools.tools_logs import LogTools

from ._error_log_test_helpers import journald_page_client, numbered_log
from .test_tools_utility_error_log_records import _kwargs


@pytest.mark.parametrize("offset", ["--5", "²"])
def test_invalid_offset_is_a_classified_validation_error(offset: str) -> None:
    with pytest.raises(ToolFailure) as error:
        _validate_log_offset("error_log", offset)
    assert error.value.code == "VALIDATION_INVALID_PARAMETER"


async def test_first_short_journal_window_keeps_unreturned_records_pageable() -> None:
    entries = numbered_log(10).splitlines()
    tools = LogTools(journald_page_client(entries))
    shown = []
    offset = 0
    for _ in range(6):
        page = await tools.get_logs(**_kwargs(limit=2, offset=offset))
        shown.extend(page["log"].splitlines())
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert shown == list(reversed(entries))


async def test_missing_supervisor_cursor_is_not_reported_as_complete_history() -> None:
    fetch = AsyncMock(return_value=JournaldPage(numbered_log(4), None))
    page = await journald_page(4, 0, fetch)
    assert not page.pagination_complete
    result = LogTools(None).error_log_result(
        page, 4, 2, None, None, "newest", 1, False, None
    )
    assert result["pagination_complete"] is False
    assert "reached the start" not in json.dumps(result)
    assert "no older history" not in json.dumps(result)


def test_core_empty_message_still_starts_a_record() -> None:
    # Core bootstrap's Formatter permits an empty message before exc_info.
    records = _split_log_records(
        [
            "2026-10-25 10:00:00.000 INFO (MainThread) [test] previous",
            "2026-10-25 10:00:01.000 ERROR (MainThread) [test] ",
            "Traceback (most recent call last):",
            "ValueError: details",
        ]
    )
    assert len(records) == 2
    assert records[1].level == "ERROR" and records[1].line_count == 3
