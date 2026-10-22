"""Only the idempotent readiness probe tolerates connections lost during restart."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import httpx
import pytest


@pytest.fixture
def restart(monkeypatch: pytest.MonkeyPatch) -> Any:
    source = Path(__file__).resolve().parents[2] / "native/restart_checks.py"
    spec = importlib.util.spec_from_file_location("native_restart_probe", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = SimpleNamespace(now=0)

    async def advance(_seconds: float) -> None:
        clock.now += 1

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(sleep=advance))
    checks = SimpleNamespace(
        path="/api/webhook/fixture",
        ready=AsyncMock(return_value={"entry_id": "same-entry"}),
        call=AsyncMock(
            side_effect=[
                {"scheduled": True, "requested": True},
                {"data": {"state": "ready"}},
                {"text": "AFTER RESTART"},
            ]
        ),
        control=AsyncMock(return_value={"error": False, "data": "ready"}),
        passed=Mock(),
    )
    states = iter(
        [
            {"StartedAt": "before", "Running": True},
            {"Running": False, "ExitCode": 100},
            {"StartedAt": "after", "Running": True},
        ]
    )

    def docker(command: str, container: str) -> Any:
        assert container == "fixture"
        return SimpleNamespace(
            stdout=json.dumps([{"State": next(states)}]) if command == "inspect" else ""
        )

    return SimpleNamespace(
        module=module, checks=checks, docker=Mock(side_effect=docker), clock=clock
    )


@pytest.mark.parametrize("failure", [httpx.ReadError, httpx.WriteError])
async def test_reset_readiness_connection_is_retried_without_restarting_again(
    restart: Any, failure: type[httpx.NetworkError]
) -> None:
    ready = {"entry_id": "same-entry"}
    restart.checks.ready.side_effect = [ready, failure("connection reset"), ready]
    await restart.module.exercise_process_restart(
        restart.checks, restart.docker, "fixture"
    )
    assert restart.checks.ready.await_count == 3
    assert (
        restart.checks.call.await_args_list.count(call("ha_restart", {"confirm": True}))
        == 1
    )
    assert restart.docker.call_args_list.count(call("start", "fixture")) == 1
    assert restart.checks.call.await_args_list[-1].args[0] == "ha_native_llm_call"


@pytest.mark.parametrize("status", [401, 500])
async def test_http_failure_during_restart_is_not_hidden(
    restart: Any, status: int
) -> None:
    request = httpx.Request("GET", "http://fixture/api/native_fixture/probe")
    failure = httpx.HTTPStatusError(
        "probe failed",
        request=request,
        response=httpx.Response(status, request=request),
    )
    restart.checks.ready.side_effect = [{"entry_id": "same-entry"}, failure]
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await restart.module.exercise_process_restart(
            restart.checks, restart.docker, "fixture"
        )
    assert caught.value is failure
    assert restart.checks.ready.await_count == 2
    restart.checks.control.assert_not_awaited()


async def test_repeated_resets_cannot_extend_the_restart_deadline(restart: Any) -> None:
    attempts = 0

    async def ready() -> dict[str, str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"entry_id": "same-entry"}
        if attempts == 10:
            raise RuntimeError("The restart deadline was repeatedly extended")
        restart.clock.now += 100
        raise httpx.ReadError("connection reset")

    restart.checks.ready.side_effect = ready
    with pytest.raises(AssertionError, match="Core did not start"):
        await restart.module.exercise_process_restart(
            restart.checks, restart.docker, "fixture"
        )
    assert 2 < restart.checks.ready.await_count < 10
    assert restart.docker.call_args_list.count(call("start", "fixture")) == 1


@pytest.mark.parametrize("failure", [httpx.ReadError, httpx.WriteError])
async def test_mutation_connection_failure_is_never_replayed(
    restart: Any, failure: type[httpx.NetworkError]
) -> None:
    error = failure("connection reset after dispatch")
    restart.checks.call.side_effect = error
    with pytest.raises(failure) as caught:
        await restart.module.exercise_process_restart(
            restart.checks, restart.docker, "fixture"
        )
    assert caught.value is error
    restart.checks.call.assert_awaited_once_with("ha_restart", {"confirm": True})
    assert call("start", "fixture") not in restart.docker.call_args_list
