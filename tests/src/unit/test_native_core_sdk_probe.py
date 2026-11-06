"""The external client follows Core's SDK API without replacing its distribution."""

import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("major,sdk_version", [("1", "1.28.1"), ("2", "2.3.0")])
@pytest.mark.parametrize("result_error", [False, True])
async def test_probe_uses_the_core_sdk_owned_http_client(
    monkeypatch, capsys, major, sdk_version, result_error
):
    from tests.native import legacy_client_probe as probe

    calls = []
    state = SimpleNamespace(model_dump_json=lambda: '{"state":"ready"}')
    setattr(state, "isError" if major == "1" else "is_error", result_error)
    initialized = SimpleNamespace()
    setattr(
        initialized,
        "protocolVersion" if major == "1" else "protocol_version",
        "2025-11-25",
    )

    class HttpClient:
        def __init__(self, **kwargs):
            calls.append(("http", kwargs))

        async def __aenter__(self):
            calls.append(("http_enter", self))
            return self

        async def __aexit__(self, *_args):
            calls.append(("http_exit", self))

    @asynccontextmanager
    async def transport(url, *, http_client):
        assert isinstance(http_client, HttpClient)
        calls.append(("transport", url))
        try:
            yield ("read", "write", "session") if major == "1" else ("read", "write")
        finally:
            calls.append(("transport_exit", url))

    session = SimpleNamespace(
        initialize=AsyncMock(return_value=initialized),
        list_tools=AsyncMock(
            return_value=SimpleNamespace(tools=[SimpleNamespace(name="ha_get_state")])
        ),
        call_tool=AsyncMock(return_value=state),
    )

    @asynccontextmanager
    async def client_session(read, write):
        assert (read, write) == ("read", "write")
        yield session

    http = SimpleNamespace(
        AsyncClient=HttpClient, Timeout=lambda *args, **kwargs: (args, kwargs)
    )
    module = SimpleNamespace(streamable_http_client=transport)
    setattr(module, "httpx" if major == "1" else "httpx2", http)
    monkeypatch.setitem(
        sys.modules, "mcp", SimpleNamespace(ClientSession=client_session)
    )
    monkeypatch.setitem(
        sys.modules, "mcp.client", SimpleNamespace(streamable_http=module)
    )
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", module)
    monkeypatch.setattr(
        probe.importlib.metadata, "version", lambda package: sdk_version
    )
    parameters = {"url": "http://fixture/mcp", "token": "private-fixture-token"}
    if result_error:
        with pytest.raises(AssertionError):
            await probe.exercise_core_client(parameters)
    else:
        await probe.exercise_core_client(parameters)
    assert calls[0][1]["headers"] == {"Authorization": "Bearer private-fixture-token"}
    assert calls[-1][0] == "http_exit" and calls[-2][0] == "transport_exit"
    session.call_tool.assert_awaited_once_with(
        "ha_get_state", {"entity_id": "sensor.native_fixture"}
    )
    output = capsys.readouterr().out
    assert "private-fixture-token" not in output
    if result_error:
        assert "PASS" not in output
    else:
        assert sdk_version in output and "2025-11-25" in output
