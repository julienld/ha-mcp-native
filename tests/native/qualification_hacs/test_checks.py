"""Bench regressions against shared tool contracts, not live owner qualification."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from ha_mcp.runtime.hacs_results import added_data
from ha_mcp.runtime.hacs_tools import HacsFacade
from ha_mcp.runtime.projection import timezone_metadata

spec = importlib.util.spec_from_file_location(
    "hacs_qualification_checks", Path(__file__).with_name("checks.py")
)
assert spec and spec.loader
checks_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks_module)


class RegistrationObserved(Exception):
    """Stop before the live owner/cache checks; this test covers their admission."""


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 401, 500])
async def test_restart_waits_for_fixture_registration_but_preserves_real_errors(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setitem(sys.modules, "run_container", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "checks", checks_module)
    driver_spec = importlib.util.spec_from_file_location(
        "hacs_restart_driver_test", Path(__file__).with_name("driver.py")
    )
    assert driver_spec and driver_spec.loader
    driver = importlib.util.module_from_spec(driver_spec)
    driver_spec.loader.exec_module(driver)
    request = httpx.Request("GET", "http://unused.invalid/api/native_fixture/probe")
    response = httpx.Response(status, request=request)
    error = httpx.HTTPStatusError(
        "fixture response", request=request, response=response
    )
    checks = SimpleNamespace(ready=AsyncMock(side_effect=[error, {"ready": True}]))
    control = AsyncMock(side_effect=RegistrationObserved())
    sleep = AsyncMock()
    monkeypatch.setattr(checks_module.asyncio, "sleep", sleep)
    if status == 404:
        with pytest.raises(RegistrationObserved):
            await driver.after_restart(checks, control)
        assert checks.ready.await_count == 2
        sleep.assert_awaited_once()
    else:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await driver.after_restart(checks, control)
        assert caught.value is error
        checks.ready.assert_awaited_once()
        sleep.assert_not_awaited()
        control.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("repository_id", ["987654321", None, "wrong-repository"])
async def test_admission_reads_shared_envelope_and_requires_registered_identity(
    repository_id: str | None,
) -> None:
    async def register(repository: str, category: str) -> dict[str, Any]:
        # These are the same result builders used by NativeHacs and the remote
        # tool. Only the owner operation is a stand-in; do not hand-flatten it.
        return {
            "success": True,
            **timezone_metadata(
                added_data(repository, category, {"id": repository_id}), "UTC"
            ),
        }

    facade = HacsFacade(SimpleNamespace(_hacs_add_repository=register))

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await getattr(facade, name)(**arguments)

    checks = SimpleNamespace(call=call, passed=Mock())
    control = AsyncMock(
        side_effect=[
            {
                "ready": True,
                "core": "2026.10.4",
                "hacs": "2.0.5",
                "owner_class": "custom_components.hacs.base.HacsBase",
                "entry_state": "loaded",
            },
            RegistrationObserved(),
        ]
    )
    expected = RegistrationObserved if repository_id == "987654321" else AssertionError
    with pytest.raises(expected):
        await checks_module.exercise_admission(checks, control, "http://unused.invalid")
    assert control.await_count == (2 if expected is RegistrationObserved else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["success", "metadata", "data"])
async def test_incomplete_tool_envelope_is_not_accepted(missing: str) -> None:
    result = {
        "success": True,
        **timezone_metadata(
            added_data(
                "qualification/hacs-owner-fixture", "integration", {"id": "987654321"}
            ),
            "UTC",
        ),
    }
    result.pop(missing)
    checks = SimpleNamespace(call=AsyncMock(return_value=result))
    with pytest.raises(AssertionError):
        await checks_module.hacs_call(
            checks, "ha_manage_hacs", {"action": "add_repository"}
        )
