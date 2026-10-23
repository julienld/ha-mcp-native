"""Verify nonempty SQLite history/statistics through the native MCP endpoint."""

import asyncio
import time
from math import isclose
from typing import Any


async def exercise_history(checks: Any) -> None:
    seeded = await checks.control("seed_history", {})
    arguments = {
        "source": "statistics",
        "entity_ids": seeded["statistic_id"],
        "start_time": seeded["start"],
        "end_time": seeded["end"],
        "period": "hour",
        "include_schema": True,
        "core_options": {"units": {"energy": "Wh"}},
    }
    result = await checks.call("ha_get_history", arguments)
    data = result["data"]
    assert data["success"] and data["core_contract"]["status"] == "available", result
    entity = data["entities"][0]
    assert entity["entity_id"] == seeded["statistic_id"], result
    assert entity["count"] == 2 and len(entity["statistics"]) == 2, result
    assert (
        entity["unit_of_measurement"] == "Wh"
        and entity["unit_source"] == "core_converter"
    ), result
    metadata = entity["statistics_metadata"]
    assert metadata["source"] == "native_fixture", result
    assert metadata["statistics_unit_of_measurement"] == "kWh", result
    for row, state, total in zip(
        entity["statistics"], (1000.0, 3000.0), (10000.0, 12000.0), strict=True
    ):
        assert isclose(row["state"], state) and isclose(row["sum"], total), result
        assert row["last_reset"] == seeded["last_reset"], result
    checks.passed(
        "native_recorder_SQLite_statistics_conversion_and_unconverted_reset_timestamps"
    )

    page = await checks.call(
        "ha_get_history", {**arguments, "limit": 1, "offset": 1, "fields": ["entities"]}
    )
    assert "core_contract" in page["data"], page
    assert page["data"]["entities"][0]["count"] == 1, page
    assert isclose(page["data"]["entities"][0]["statistics"][0]["state"], 3000.0), page
    checks.passed("native_recorder_statistics_pagination_preserves_Core_schema")

    deadline = time.monotonic() + 15
    while True:
        history = await checks.call(
            "ha_get_history",
            {
                "entity_ids": seeded["state_entity_id"],
                "start_time": seeded["state_start"],
                "order": "asc",
                "minimal_response": False,
                "significant_changes_only": False,
            },
        )
        states = history["data"]["entities"][0]["states"]
        if len(states) >= 2 and [row["state"] for row in states][-2:] == ["1", "2"]:
            break
        # Recorder may have dequeued a write before its commit is visible.
        # Repeat only the public read, never the seeded state mutations.
        assert time.monotonic() < deadline, history
        await asyncio.sleep(0.1)
    assert states[-1]["attributes"]["unit_of_measurement"] == "kWh", history
    assert "last_updated" in states[-1] and "lu" not in states[-1], history
    checks.passed(
        "native_recorder_SQLite_state_history_retains_attributes_and_local_timestamps"
    )

    summary = await checks.call(
        "ha_get_history",
        {
            "entity_ids": seeded["state_entity_id"],
            "start_time": seeded["state_start"],
            "summary": True,
        },
    )
    assert summary["data"]["entities"][0]["summary"], summary
    assert "states" not in summary["data"]["entities"][0], summary
    checks.passed("native_recorder_SQLite_state_summary")
