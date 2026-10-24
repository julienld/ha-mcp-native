"""A writable calendar's internal failure must fail its E2E, never become a skip."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.src.e2e.workflows.calendar.test_calendar import (
    TestCalendarEventLifecycle as _CalendarLifecycle,
)


async def test_calendar_creation_regression_is_not_reported_as_missing_capability(
    monkeypatch,
):
    failure = RuntimeError("Core serializer unexpectedly unavailable")
    client = SimpleNamespace(call_tool=AsyncMock(side_effect=failure))
    monkeypatch.setattr(
        _CalendarLifecycle,
        "_find_writable_calendar",
        AsyncMock(return_value="calendar.local_e2e_test"),
    )
    with pytest.raises(BaseException) as observed:
        await _CalendarLifecycle().test_create_calendar_event(client, Mock())
    assert observed.value is failure
