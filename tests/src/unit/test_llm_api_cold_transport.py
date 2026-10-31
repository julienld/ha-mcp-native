"""Cold legacy Assist transport construction stays off Home Assistant's loop."""

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import llm_api  # noqa: E402

from ._llm_api_helpers import make_hass  # noqa: E402


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
