"""A failed cleanup must not conceal the original runtime-switch failure."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_CHECKS = Path(__file__).resolve().parents[2] / "native"


async def test_rollback_keeps_primary_failure_and_reports_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(_CHECKS))
    rollback = importlib.import_module("legacy_rollback_checks")
    monkeypatch.setattr(rollback, "_register", AsyncMock(return_value="fixture-client"))
    monkeypatch.setattr(
        rollback,
        "prepare_grant",
        AsyncMock(return_value={"refresh_token": "fixture-envelope"}),
    )
    monkeypatch.setattr(
        rollback,
        "_ready",
        AsyncMock(side_effect=AssertionError("native generation failed")),
    )
    checks = SimpleNamespace(
        path="/fixture",
        call=AsyncMock(return_value={"data": {"state": "on"}}),
        control=AsyncMock(side_effect=[{}, RuntimeError("return cleanup failed")]),
        client=SimpleNamespace(
            post=AsyncMock(return_value=SimpleNamespace(status_code=503))
        ),
    )
    with pytest.raises(AssertionError, match="native generation failed") as failure:
        await rollback.exercise_legacy_rollback(checks)
    assert any("cleanup" in note for note in failure.value.__notes__)
    checks.client.post.assert_awaited_once()


async def test_successful_rollback_still_fails_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(_CHECKS))
    rollback = importlib.import_module("legacy_rollback_checks")
    checks = SimpleNamespace(
        client=SimpleNamespace(
            post=AsyncMock(return_value=SimpleNamespace(status_code=503))
        )
    )
    with pytest.raises(ExceptionGroup, match="Rollback cleanup failed"):
        await rollback._cleanup_rollback(
            checks, "fixture-envelope", restore=False, primary=None
        )
