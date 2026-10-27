"""Legacy Assist uses the bundled SDK without importing Core's shared client."""

import asyncio
import subprocess
import sys
import textwrap
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx2
import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import llm_api  # noqa: E402


def test_legacy_import_is_cold_then_prewarms_only_private_sdk_off_loop() -> None:
    script = """
        import asyncio, sys, threading
        from importlib.abc import MetaPathFinder
        from types import ModuleType, SimpleNamespace
        from pathlib import Path
        from tests.src.unit._embedded_stubs import install
        install()
        package = ModuleType('custom_components.ha_mcp_tools')
        package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
        sys.modules[package.__name__] = package
        main_thread = threading.get_ident()
        class Guard(MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'mcp' or fullname.startswith('mcp.'):
                    raise AssertionError('Imported the shared SDK: ' + fullname)
                if fullname.startswith('ha_mcp._vendor.mcp'):
                    assert threading.get_ident() != main_thread, fullname
        sys.meta_path.insert(0, Guard())
        from custom_components.ha_mcp_tools import llm_api
        assert not any(name.startswith('ha_mcp._vendor.mcp') for name in sys.modules)
        llm_api._schema_converter = lambda: None
        async def run():
            hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)
            assert await llm_api.async_probe_mcp_sdk(hass)
        asyncio.run(run())
        assert 'ha_mcp._vendor.mcp.client.session' in sys.modules
        assert 'ha_mcp._vendor.mcp.client.streamable_http' in sys.modules
    """
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


async def test_broken_private_dependency_never_falls_back_to_shared_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported = []

    def load(name: str) -> Any:
        imported.append(name)
        if name.startswith("ha_mcp._vendor.mcp"):
            raise ModuleNotFoundError(
                "missing transport dependency", name="missing_dep"
            )
        return SimpleNamespace()

    monkeypatch.setattr(llm_api.importlib, "import_module", load)
    monkeypatch.setattr(llm_api, "_schema_converter", lambda: None)
    hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)
    assert not await llm_api.async_probe_mcp_sdk(hass)
    assert imported == ["ha_mcp._vendor.mcp.client.session"]


def test_private_protocol_error_is_classified_without_hiding_mixed_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MCPError(Exception):
        pass

    monkeypatch.setitem(
        sys.modules, "ha_mcp._vendor.mcp", SimpleNamespace(MCPError=MCPError)
    )
    assert llm_api._is_transport_failure(MCPError("protocol failed"))
    assert not llm_api._is_transport_failure(
        ExceptionGroup("mixed", [MCPError("protocol failed"), ValueError("bug")])
    )


async def test_sessions_use_current_generation_and_close_each_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, closed, clients = [], [], []

    @asynccontextmanager
    async def transport(url: str, *, http_client: Any) -> Any:
        assert url.startswith("http://127.0.0.1:")
        clients.append(http_client)
        yield "reader", "writer"

    module = ModuleType("ha_mcp._vendor.mcp.client.streamable_http")
    module.streamable_http_client = transport
    module.httpx2 = httpx2
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", module)

    def shared_session(*_args: Any) -> Any:
        raise AssertionError("Legacy Assist selected Core's shared session")

    monkeypatch.setitem(
        sys.modules, "mcp.client.session", SimpleNamespace(ClientSession=shared_session)
    )
    for generation in (1, 2):

        class Session:
            def __init__(
                self, _reader: Any, _writer: Any, generation_id: int = generation
            ) -> None:
                self.generation = generation_id

            async def __aenter__(self) -> Any:
                entered.append(self.generation)
                return self

            async def __aexit__(self, *_args: Any) -> None:
                closed.append(self.generation)

            async def initialize(self) -> Any:
                return SimpleNamespace(instructions="same wire contract")

        monkeypatch.setitem(
            sys.modules,
            "ha_mcp._vendor.mcp.client.session",
            SimpleNamespace(ClientSession=Session),
        )
        async with llm_api._mcp_session("http://127.0.0.1:9584/fixture") as (
            session,
            _init,
        ):
            assert session.generation == generation
            assert not clients[-1].is_closed
    assert entered == closed == [1, 2]
    assert all(client.is_closed for client in clients)
