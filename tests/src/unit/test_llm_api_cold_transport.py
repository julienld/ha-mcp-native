"""Cold legacy Assist transport construction stays off Home Assistant's loop."""

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import llm_api  # noqa: E402

from ._llm_api_helpers import fake_session, make_api, make_hass  # noqa: E402


@pytest.mark.parametrize("operation", ["list", "call-before-list", "call-before-write"])
async def test_export_disabled_inside_anyio_session_remains_a_home_assistant_error(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    hass = make_hass()
    api = make_api(hass)
    api.entry = SimpleNamespace(options={"enable_llm_api": True})
    session = fake_session(monkeypatch)
    ungrouped_session = llm_api._mcp_session

    async def disable_after_list() -> SimpleNamespace:
        api.entry.options["enable_llm_api"] = False
        return session.list_tools.return_value

    if operation == "call-before-write":
        session.list_tools.side_effect = disable_after_list

    @asynccontextmanager
    async def grouped_session(url: str) -> AsyncIterator[Any]:
        async with (
            anyio.create_task_group(),
            anyio.create_task_group(),
            ungrouped_session(url) as opened,
        ):
            if operation != "call-before-write":
                api.entry.options["enable_llm_api"] = False
            yield opened

    monkeypatch.setattr(llm_api, "_mcp_session", grouped_session)
    with pytest.raises(llm_api.HomeAssistantError, match="export is disabled"):
        if operation == "list":
            await api.async_get_api_instance(llm_api.llm.LLMContext())
        else:
            await llm_api._forward_tool_call(
                hass, api.server_url, "ha_search", {}, check_enabled=api._check_enabled
            )
    session.call_tool.assert_not_awaited()
    if operation == "call-before-write":
        session.list_tools.assert_awaited_once()
    else:
        session.list_tools.assert_not_awaited()


@pytest.mark.parametrize("operation", ["list", "call"])
@pytest.mark.parametrize("other_error", [ValueError, OSError, asyncio.CancelledError])
async def test_mixed_home_assistant_error_groups_propagate_unchanged(
    monkeypatch: pytest.MonkeyPatch, operation: str, other_error: type[BaseException]
) -> None:
    error = BaseExceptionGroup(
        "session failure",
        [
            ExceptionGroup("export guard", [llm_api.HomeAssistantError("disabled")]),
            other_error("other failure"),
        ],
    )
    fake_session(monkeypatch, raise_on_open=error)
    hass = make_hass()
    api = make_api(hass)
    with pytest.raises(BaseExceptionGroup) as failure:
        if operation == "list":
            await api.async_get_api_instance(llm_api.llm.LLMContext())
        else:
            await llm_api._forward_tool_call(hass, api.server_url, "ha_search", {})
    assert failure.value is error


@pytest.mark.parametrize("operation", ["list", "call"])
async def test_nested_groups_with_only_home_assistant_errors_preserve_the_first_error(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    first = llm_api.HomeAssistantError("export disabled")
    fake_session(
        monkeypatch,
        raise_on_open=ExceptionGroup(
            "session failure",
            [ExceptionGroup("guard", [first]), llm_api.HomeAssistantError("disabled")],
        ),
    )
    hass = make_hass()
    api = make_api(hass)
    with pytest.raises(llm_api.HomeAssistantError) as failure:
        if operation == "list":
            await api.async_get_api_instance(llm_api.llm.LLMContext())
        else:
            await llm_api._forward_tool_call(hass, api.server_url, "ha_search", {})
    assert failure.value is first


async def test_v2_transport_warms_metadata_off_loop_and_closes_its_httpx2_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.metadata
    import sys
    from types import ModuleType

    import httpx2

    opened: dict[str, Any] = {}

    @asynccontextmanager
    async def _v2_client(
        url: str, *, http_client: Any = None
    ) -> AsyncIterator[tuple[str, str]]:
        opened["http_client"] = http_client
        yield "read-stream", "write-stream"  # 2.x yields two streams

    fake_transport = ModuleType("ha_mcp._vendor.mcp.client.streamable_http")
    fake_transport.streamable_http_client = _v2_client  # type: ignore[attr-defined]
    fake_transport.httpx2 = httpx2  # type: ignore[attr-defined]

    class _FakeClientSession:
        def __init__(self, read_stream: Any, write_stream: Any) -> None:
            opened["streams"] = (read_stream, write_stream)

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc_info: Any) -> bool:
            return False

        async def initialize(self) -> SimpleNamespace:
            return SimpleNamespace(instructions="v2")

    fake_session_mod = ModuleType("ha_mcp._vendor.mcp.client.session")
    fake_session_mod.ClientSession = _FakeClientSession  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "ha_mcp._vendor.mcp.client.streamable_http", fake_transport
    )
    monkeypatch.setitem(
        sys.modules, "ha_mcp._vendor.mcp.client.session", fake_session_mod
    )

    # Reproduce the cold httpcore2 import that reads distribution metadata
    # from AsyncClient construction on the first Assist exchange.
    monkeypatch.delitem(sys.modules, "httpcore2", raising=False)
    version = importlib.metadata.version
    metadata_threads = []
    loop_thread = threading.get_ident()

    def checked_version(name: str) -> str:
        if name == "httpcore2":
            metadata_threads.append(threading.get_ident())
            assert threading.get_ident() != loop_thread, "metadata read on HA loop"
        return version(name)

    monkeypatch.setattr(importlib.metadata, "version", checked_version)
    monkeypatch.setattr(llm_api, "_schema_converter", lambda: None)
    hass = make_hass()
    hass.async_add_executor_job = AsyncMock(side_effect=asyncio.to_thread)
    assert await llm_api.async_probe_mcp_sdk(hass)

    async with llm_api._mcp_session("http://127.0.0.1:9584/private_x"):
        client = opened["http_client"]
        assert isinstance(client, httpx2.AsyncClient)
        assert client.trust_env is False
        assert client.timeout == httpx2.Timeout(llm_api._CALL_TOOL_TIMEOUT_SECONDS)
    assert opened["streams"] == ("read-stream", "write-stream")
    assert metadata_threads and all(
        thread != loop_thread for thread in metadata_threads
    )
    assert client.is_closed
