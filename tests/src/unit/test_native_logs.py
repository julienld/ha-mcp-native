"""Direct log owners retain windows, policy and cleanup without HA loopback."""

import asyncio
import copy
import importlib
import importlib.machinery
import importlib.util
import json
import re
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.log_tools import LogsFacade
from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


def _log_line(kind: str, index: int) -> str:
    return f"2026-10-25 10:00:00.000 ERROR (MainThread) [native.test] {kind} {index}"


class _SupervisorResponse:
    status = 200

    def __init__(self, world: Any, text: str, cursor: str | None) -> None:
        self.world = world
        self.value = text
        self.headers = {"X-First-Cursor": cursor} if cursor else {}

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.world.closed += 1

    async def text(self) -> str:
        if self.world.request_error:
            raise self.world.request_error
        if getattr(self.world, "release", None) is not None:
            self.world.started.set()
            await self.world.release.wait()
        return self.value


@pytest.fixture
def log_url_library(monkeypatch: pytest.MonkeyPatch) -> Any:
    """OAuth collection stubs may replace yarl with a URL lacking joinpath."""
    previous = {
        name: module
        for name, module in sys.modules.items()
        if name == "yarl" or name.startswith("yarl.")
    }
    spec = importlib.machinery.PathFinder.find_spec("yarl")
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "yarl", package)
    try:
        spec.loader.exec_module(package)
        yield package
    finally:
        for name in list(sys.modules):
            if name == "yarl" or name.startswith("yarl."):
                sys.modules.pop(name)
        sys.modules.update(previous)


@pytest.fixture
async def logs(
    native: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log_url_library: Any
) -> Any:
    world = SimpleNamespace(
        journal_entries=[_log_line("journal", index) for index in range(300)],
        entries=[
            {
                "name": "native.example",
                "message": ["message " * 500],
                "level": "WARNING",
                "timestamp": 2,
                "count": 8,
                "future": True,
            }
        ],
        file_access=True,
        request_error=None,
        requests=[],
        closed=0,
        threads=[],
    )

    async def execute(function: Any, *args: Any) -> Any:
        def run() -> Any:
            world.threads.append(threading.get_ident())
            return function(*args)

        return await asyncio.to_thread(run)

    path = tmp_path / "core.log"
    path.write_text(
        "\n".join(_log_line("line", index) for index in range(300)) + "\n",
        encoding="utf-8",
    )
    hass = SimpleNamespace(
        data={
            "error-log": str(path),
            "system_log": SimpleNamespace(
                records=SimpleNamespace(to_list=lambda: copy.deepcopy(world.entries))
            ),
            "logger": SimpleNamespace(
                settings=SimpleNamespace(
                    async_get_integration_domains=lambda: {"configured"}
                )
            ),
        },
        config=SimpleNamespace(
            components=set(), config_dir=str(tmp_path), time_zone="UTC"
        ),
        config_entries=SimpleNamespace(
            flow=SimpleNamespace(
                async_progress=lambda: [
                    {"handler": "discovered", "context": {"source": "discovery"}}
                ]
            )
        ),
        services=SimpleNamespace(has_service=lambda *_: world.file_access),
        async_add_executor_job=execute,
    )
    user = authorize(hass)
    monkeypatch.setattr(
        sys.modules["homeassistant.const"],
        "KEY_DATA_LOGGING",
        "error-log",
        raising=False,
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.config_entries"],
        "DISCOVERY_SOURCES",
        {"discovery"},
        raising=False,
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.system_log",
        SimpleNamespace(DOMAIN="system_log"),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.logger.helpers",
        SimpleNamespace(
            DATA_LOGGER="logger",
            get_logger=lambda _: SimpleNamespace(getEffectiveLevel=lambda: 30),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.setup",
        SimpleNamespace(async_get_loaded_integrations=lambda _: {"loaded"}),
    )
    token = "fake-supervisor-secret"

    def request(method: str, url: Any, **kwargs: Any) -> _SupervisorResponse:
        world.requests.append((method, url, kwargs))
        match = re.fullmatch(
            r"entries=([^:]*):(-?\d+):(\d+)", kwargs["headers"]["Range"]
        )
        assert match is not None
        cursor, skip_value, count_value = match.groups()
        skip, count = int(skip_value), int(count_value)
        all_lines = world.journal_entries
        anchor = int(cursor[1:]) if cursor else len(all_lines) - 1
        start = max(0, anchor + skip)
        block = all_lines[start : start + count]
        return _SupervisorResponse(
            world, "\n".join(block), f"e{start}" if block else None
        )

    async def status(response: Any) -> None:
        assert response.status == 200

    transport = SimpleNamespace(
        session=SimpleNamespace(closed=False, request=request),
        token=token,
        api_host="http://supervisor",
        _raise_on_status=status,
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.hassio",
        SimpleNamespace(
            get_supervisor_client=lambda _: SimpleNamespace(_client=transport)
        ),
    )
    backend = native.NativeBackend(hass)
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.logs"
    )
    binder = importlib.import_module("custom_components.ha_mcp_tools.native_log_tools")
    manager = SimpleNamespace(hass=hass, backend=backend)
    return SimpleNamespace(
        world=world,
        hass=hass,
        user=user,
        path=path,
        directory=tmp_path,
        engine=Engine(backend, [binder.logs_definition(manager)]),
        module=module,
        backend=backend,
        token=token,
    )


async def _get(logs: Any, **arguments: Any) -> Any:
    return await logs.engine.call(
        "ha_get_logs", arguments, CallContext("actor", read_only=True)
    )


async def test_absent_supervisor_preserves_requested_slug_and_recovery_guidance(
    logs: Any,
) -> None:
    result = await _get(logs, source="supervisor", slug="nonexistent_addon_slug")
    assert result.is_error and result.status == "not_started"
    assert result.data["source"] == "supervisor"
    assert result.data["slug"] == "nonexistent_addon_slug"
    suggestions = result.data["error"]["suggestions"]
    assert any("ha_get_app" in hint for hint in suggestions)
    assert any("Supervisor" in hint for hint in suggestions)
    assert not logs.world.requests


async def test_system_log_preserves_core_counts_and_bounds_messages_off_loop(
    logs: Any,
) -> None:
    result = await _get(logs, source="system")
    assert not result.is_error, result.data
    entry = result.data["entries"][0]
    assert entry["count"] == 8 and entry["future"] is True
    assert len(entry["message"][0]) < len(logs.world.entries[0]["message"][0])
    assert result.data["truncated_messages"] == 1
    assert logs.world.threads and all(
        thread != threading.get_ident() for thread in logs.world.threads
    )


async def test_logger_reads_loaded_discovered_and_configured_core_domains(
    logs: Any,
) -> None:
    result = await _get(logs, source="logger")
    assert not result.is_error, result.data
    assert [item["domain"] for item in result.data["loggers"]] == [
        "configured",
        "discovered",
        "loaded",
    ]
    assert all(item["level"] == "WARNING" for item in result.data["loggers"])


async def test_core_configured_file_pages_from_tail_without_skipping_lines(
    logs: Any,
) -> None:
    first = await _get(logs, source="error_log", limit=2)
    assert not first.is_error, first.data
    assert first.data["log"].splitlines() == [
        _log_line("line", 299),
        _log_line("line", 298),
    ]
    assert first.data["has_more"] and first.data["next_offset"] == 2
    second = await _get(
        logs, source="error_log", limit=2, offset=first.data["next_offset"]
    )
    assert second.data["log"].splitlines() == [
        _log_line("line", 297),
        _log_line("line", 296),
    ]
    last = await _get(logs, source="error_log", limit=2, offset=999)
    assert last.data["log"] == "" and not last.data["has_more"]


async def test_fault_log_keeps_tools_entry_gate_and_missing_file_semantics(
    logs: Any,
) -> None:
    logs.world.file_access = False
    denied = await _get(logs, source="fault_log")
    assert denied.is_error and denied.data["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    logs.world.file_access = True
    empty = await _get(logs, source="fault_log")
    assert not empty.is_error and not empty.data["crash_recorded"], empty.data
    (logs.directory / "home-assistant.log.fault").write_text(
        "Fatal Python error: Aborted\n  first frame\n\nFatal Python error: Segmentation fault\n  second frame\n",
        encoding="utf-8",
    )
    present = await _get(logs, source="fault_log")
    assert not present.is_error and present.data["crash_recorded"], present.data
    assert present.data["log"].index("Segmentation fault") < present.data["log"].index(
        "Aborted"
    )


async def test_fault_file_applies_existing_path_policy_before_read(
    logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = importlib.import_module("custom_components.ha_mcp_tools")
    monkeypatch.setattr(root, "_is_path_allowed_for_read", lambda *_: False)
    read = Mock(side_effect=AssertionError("Disallowed file was opened"))
    monkeypatch.setattr(root, "_read_log_tail_sync", read)
    result = await _get(logs, source="fault_log")
    assert result.is_error and result.data["error"]["code"] == "AUTH_FORBIDDEN"
    read.assert_not_called()


async def test_supervisor_uses_existing_transport_and_real_range_pagination(
    logs: Any,
) -> None:
    logs.hass.config.components.add("hassio")
    result = await _get(logs, source="error_log", limit=2)
    assert not result.is_error, result.data
    assert result.data["log"].splitlines() == [
        _log_line("journal", 299),
        _log_line("journal", 298),
    ]
    assert result.data["has_more"] and len(logs.world.requests) == 2
    assert all(request[1].path == "/core/logs" for request in logs.world.requests)
    assert logs.world.requests[0][2]["headers"]["Range"] == "entries=:-201:202"
    assert logs.world.closed == 2
    logs.world.journal_entries.append(_log_line("journal", 300))
    second = await _get(
        logs, source="error_log", limit=2, offset=result.data["next_offset"]
    )
    assert not second.is_error, second.data
    assert second.data["log"].splitlines() == [
        _log_line("journal", 297),
        _log_line("journal", 296),
    ]
    assert logs.world.closed == len(logs.world.requests)
    app = await _get(logs, source="supervisor", slug="core_ssh", limit=2)
    assert (
        not app.is_error and logs.world.requests[-1][1].path == "/addons/core_ssh/logs"
    ), app.data
    service = await _get(logs, source="system_service", slug="dns", limit=2)
    assert not service.is_error and logs.world.requests[-1][1].path == "/dns/logs", (
        service.data
    )


async def test_supervisor_slug_cannot_escape_the_logs_endpoint(logs: Any) -> None:
    logs.hass.config.components.add("hassio")
    result = await _get(logs, source="supervisor", slug="../backups", limit=2)
    assert result.is_error and logs.world.requests == []


async def test_supervisor_failure_does_not_disclose_transport_credentials(
    logs: Any,
) -> None:
    logs.hass.config.components.add("hassio")
    logs.world.request_error = RuntimeError(f"Request held {logs.token}")
    result = await _get(logs, source="system_service", slug="dns")
    assert result.is_error and result.data["error"]["code"] == "SUPERVISOR_LOG_FAILED"
    assert logs.token not in json.dumps(result.data)
    assert logs.world.closed == 1


async def test_revocation_while_file_is_read_prevents_log_disclosure(logs: Any) -> None:
    execute = logs.hass.async_add_executor_job

    async def revoke(function: Any, *args: Any) -> Any:
        result = await execute(function, *args)
        logs.user.is_active = False
        return result

    logs.hass.async_add_executor_job = revoke
    result = await _get(logs, source="error_log")
    assert result.is_error and result.data["error"]["code"] == "AUTH_FORBIDDEN"
    assert "line 9" not in json.dumps(result.data)


async def test_logbook_uses_core_processor_names_and_recorder_executor(
    logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.websocket_api.logbook"
    )
    logs.hass.data["logbook"] = object()
    clock = SimpleNamespace(
        parse_datetime=datetime.fromisoformat,
        as_utc=lambda value: value.astimezone(UTC),
        utcnow=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(module, "dt_util", clock)
    monkeypatch.setattr(module, "async_filter_entities", lambda _hass, ids: ids)
    monkeypatch.setattr(
        module, "async_determine_event_types", lambda *_: ["state_changed"]
    )
    thread_ids = []

    def events(*_args: Any) -> list:
        thread_ids.append(threading.get_ident())
        return [
            {
                "when": "2024-01-01T00:00:00+00:00",
                "entity_id": "light.example",
                "state": "on",
                "name": "Core resolved name",
                "future": True,
            }
        ]

    owner = Mock(return_value=SimpleNamespace(get_events=events))
    monkeypatch.setattr(module, "EventProcessor", owner)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.recorder",
        SimpleNamespace(get_instance=lambda _: logs.hass),
    )
    result = await _get(
        logs,
        source="logbook",
        entity_id="light.example",
        end_time="2024-01-01T01:00:00+00:00",
        compact=False,
    )
    assert not result.is_error, result.data
    assert result.data["data"]["entries"][0]["name"] == "Core resolved name"
    assert result.data["data"]["entries"][0]["future"] is True
    assert owner.call_args.kwargs == {"timestamp": False, "include_entity_name": True}
    assert thread_ids and thread_ids[0] != threading.get_ident()


async def test_cancelled_supervisor_read_retains_response_until_drain(
    logs: Any,
) -> None:
    logs.hass.config.components.add("hassio")
    logs.world.started, logs.world.release = asyncio.Event(), asyncio.Event()
    service = logs.module.NativeLogs(
        logs.hass, CallContext("actor"), authorize=logs.backend.authorize
    )
    task = asyncio.create_task(
        LogsFacade(service.get).ha_get_logs(source="supervisor", slug="core_ssh")
    )
    await asyncio.wait_for(logs.world.started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not logs.world.closed
    logs.world.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert logs.world.closed == 1


@pytest.mark.parametrize("supervised", [False, True])
async def test_record_projection_and_journal_split_run_off_the_core_loop(
    logs: Any, monkeypatch: pytest.MonkeyPatch, supervised: bool
) -> None:
    from ha_mcp.runtime import error_log_parsing, error_log_read, log_results

    loop_thread = threading.get_ident()
    split = error_log_parsing._split_log_records
    calls = []

    def checked(lines: list[str]) -> Any:
        assert threading.get_ident() != loop_thread
        calls.append(len(lines))
        return split(lines)

    monkeypatch.setattr(error_log_read, "_split_log_records", checked)
    monkeypatch.setattr(log_results, "_split_log_records", checked)
    if supervised:
        logs.hass.config.components.add("hassio")
        text = _SupervisorResponse.text

        class OffLoopText(str):
            def splitlines(self, keepends: bool = False) -> list[str]:
                assert threading.get_ident() != loop_thread
                return super().splitlines(keepends)

        async def detached(response: Any) -> str:
            return OffLoopText(await text(response))

        monkeypatch.setattr(_SupervisorResponse, "text", detached)
    result = await _get(logs, source="error_log", limit=2)
    assert not result.is_error, result.data
    assert calls and result.data["returned_records"] == 2


@pytest.mark.parametrize("failure_code", ["RESOURCE_NOT_FOUND", "AUTH_FORBIDDEN"])
async def test_native_widening_retains_first_page_only_for_read_failures(
    logs: Any, monkeypatch: pytest.MonkeyPatch, failure_code: str
) -> None:
    from ha_mcp.runtime.log_window import ErrorLogPage
    from ha_mcp.tool_api import ToolFailure

    reader = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.log_files"
    )
    calls = []

    async def read(_self: Any, lines: int, offset: int | str) -> ErrorLogPage:
        calls.append(lines)
        if len(calls) > 1:
            raise ToolFailure(failure_code, "Read unavailable", status="not_started")
        return ErrorLogPage("  older frame\n" + _log_line("new", 1), True)

    monkeypatch.setattr(reader.NativeLogFiles, "error_log", read)
    result = await _get(logs, source="error_log", limit=10)
    assert len(calls) == 2 and calls[0] < calls[1] <= 2000
    if failure_code == "AUTH_FORBIDDEN":
        assert result.is_error and result.data["error"]["code"] == failure_code
        assert "older frame" not in json.dumps(result.data)
    else:
        assert not result.is_error, result.data
        assert any("wider read" in warning for warning in result.data["warnings"])
        assert "new 1" in result.data["log"]
