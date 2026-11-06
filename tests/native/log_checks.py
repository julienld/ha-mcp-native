"""Qualify available Core log sources; this Container probe does not cover HAOS."""

import asyncio
import time
from typing import Any


async def exercise_logs(checks: Any) -> None:
    marker = (await checks.control("seed_logs", {}))["marker"]
    system = await checks.call("ha_get_logs", {"source": "system", "search": marker})
    assert system["entries"] and system["entries"][0]["count"] >= 2, system
    assert marker in str(system["entries"][0]["message"]), system
    checks.passed("native_system_log_reads_Core_deduplication_counts")

    logger = await checks.call(
        "ha_get_logs", {"source": "logger", "search": "native_fixture"}
    )
    assert any(
        item["domain"] == "native_fixture" and item["level"]
        for item in logger["loggers"]
    ), logger
    checks.passed("native_logger_reads_loaded_integration_effective_levels")

    deadline = time.monotonic() + 10
    while True:
        error_log = await checks.call(
            "ha_get_logs", {"source": "error_log", "search": marker}
        )
        if marker in error_log["log"]:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(error_log)
        await asyncio.sleep(0.1)
    assert error_log["returned_lines"] >= 2, error_log
    checks.passed("native_error_log_reads_the_real_Core_configured_file")

    logbook = await checks.call(
        "ha_get_logs", {"source": "logbook", "search": marker, "compact": False}
    )
    entries = logbook["data"]["entries"]
    assert entries and any(marker in item.get("message", "") for item in entries), (
        logbook
    )
    assert any(item.get("name") == "Native log fixture" for item in entries), logbook
    checks.passed("native_logbook_uses_Core_EventProcessor_with_recorded_names")
