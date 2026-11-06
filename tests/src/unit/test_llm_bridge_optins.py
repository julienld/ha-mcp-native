"""Both optional Core LLM bridges require an independent saved choice."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import fake_llm_apis, install

install()

from custom_components.ha_mcp_tools import llm_api, native_llm, native_llm_import  # noqa: E402
from ha_mcp.tool_api import CallContext  # noqa: E402

from ._llm_api_helpers import make_hass  # noqa: E402


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("options", [{}, {"enable_llm_api": False}])
async def test_disabled_export_never_registers_or_prepares_sdk(
    monkeypatch: pytest.MonkeyPatch, native: bool, options: dict
) -> None:
    hass = make_hass()
    entry = SimpleNamespace(options=options, entry_id="saved-id", title="Saved title")
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(llm_api, "async_probe_mcp_sdk", probe)
    if native:
        await native_llm.async_register_native_llm(hass, entry, SimpleNamespace())
    else:
        await llm_api.async_register_llm_api(hass, entry, port=9584, secret_path="/x")
    assert not fake_llm_apis(hass)
    probe.assert_not_awaited()
    hass.async_add_executor_job.assert_not_awaited()


@pytest.mark.parametrize(
    "options",
    [{}, {"enable_native_llm_import": False, "native_import_apis": ["assist"]}],
)
async def test_disabled_import_never_registers_or_resolves_core_api(
    monkeypatch: pytest.MonkeyPatch, options: dict
) -> None:
    manager = SimpleNamespace(
        entry=SimpleNamespace(options=options),
        mcp=MagicMock(),
        backend=SimpleNamespace(authorize=AsyncMock()),
        hass=object(),
    )
    lookup = AsyncMock()
    monkeypatch.setattr(native_llm_import, "llm", SimpleNamespace(async_get_api=lookup))
    native_llm_import.register_import_tools(manager)
    manager.mcp.tool.assert_not_called()
    with pytest.raises(PermissionError):
        await native_llm_import.resolve_api(manager, "assist", CallContext("alice"))
    lookup.assert_not_awaited()


async def test_import_discovery_rechecks_opt_in_after_core_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = {"enable_native_llm_import": True, "native_import_apis": ["assist"]}
    manager = SimpleNamespace(
        entry=SimpleNamespace(options=options),
        backend=SimpleNamespace(authorize=AsyncMock()),
        hass=object(),
    )

    async def resolve(*_args: object) -> SimpleNamespace:
        options["enable_native_llm_import"] = False
        return SimpleNamespace(
            api=SimpleNamespace(id="assist"), api_prompt="private", tools=[]
        )

    monkeypatch.setattr(
        native_llm_import,
        "llm",
        SimpleNamespace(async_get_api=resolve, LLMContext=SimpleNamespace),
    )
    monkeypatch.setattr(native_llm_import, "Context", SimpleNamespace)
    monkeypatch.setattr(native_llm_import.llm, "LLMContext", SimpleNamespace)
    with pytest.raises(PermissionError):
        await native_llm_import.inspect_api(manager, "assist", CallContext("alice"))


@pytest.mark.parametrize("during_connect", [False, True])
async def test_cached_legacy_tool_refuses_opt_out_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, during_connect: bool
) -> None:
    entry = SimpleNamespace(
        options={"enable_llm_api": True}, entry_id="old", title="Old"
    )
    api = llm_api._apis_for_mode(make_hass(), entry, "http://unused", "full")[0]
    tool = api._mirror(
        SimpleNamespace(name="write", description="Write", meta=None), {}
    )
    call = AsyncMock()

    @asynccontextmanager
    async def connect(_url: str):
        entry.options["enable_llm_api"] = False
        yield SimpleNamespace(call_tool=call), None

    monkeypatch.setattr(llm_api, "_mcp_session", connect)
    if not during_connect:
        entry.options["enable_llm_api"] = False
    with pytest.raises(llm_api.HomeAssistantError, match="export is disabled"):
        await tool.async_call(
            make_hass(), SimpleNamespace(tool_args={}), SimpleNamespace()
        )
    call.assert_not_awaited()


def test_bridge_migration_preserves_only_explicit_prior_choices() -> None:
    from custom_components.ha_mcp_tools.llm_bridge_options import bridge_options

    assert bridge_options({}) == {
        "enable_llm_api": False,
        "enable_native_llm_import": False,
    }
    old = {
        "enable_llm_api": True,
        "native_import_apis": ["assist"],
        "runtime_mode": "legacy",
    }
    migrated = bridge_options(old)
    assert migrated == {**old, "enable_native_llm_import": True}
    assert bridge_options(migrated) == migrated
    disabled = {**old, "enable_native_llm_import": False}
    assert bridge_options(disabled) == disabled


@pytest.mark.parametrize("invalid", ["assist", True, None, [""], ["ha_mcp_tools-own"]])
def test_invalid_import_selection_does_not_become_an_opt_in(invalid: object) -> None:
    from custom_components.ha_mcp_tools.llm_bridge_options import bridge_options

    assert (
        bridge_options({"native_import_apis": invalid})["enable_native_llm_import"]
        is False
    )


@pytest.mark.parametrize("native", [False, True])
async def test_explicit_export_preserves_api_ids_and_opt_out_unregisters(
    monkeypatch: pytest.MonkeyPatch, native: bool
) -> None:
    hass = make_hass()
    entry = SimpleNamespace(
        options={"enable_llm_api": True, "llm_api_exposure": "both"},
        entry_id="historic",
        title="Historic",
    )
    monkeypatch.setattr(llm_api, "async_probe_mcp_sdk", AsyncMock(return_value=True))
    monkeypatch.setattr(native_llm, "_schema_converter", lambda: None)

    async def register() -> None:
        if native:
            await native_llm.async_register_native_llm(
                hass, entry, SimpleNamespace(entry=entry)
            )
        else:
            await llm_api.async_register_llm_api(
                hass, entry, port=9584, secret_path="/x"
            )

    await register()
    assert set(fake_llm_apis(hass)) == {
        "ha_mcp_tools-historic",
        "ha_mcp_tools-historic-toolsearch",
    }
    entry.options["enable_llm_api"] = False
    await register()
    assert not fake_llm_apis(hass)


async def test_cached_search_refuses_export_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ._llm_api_helpers import fake_session, tool_entry

    hass = make_hass()
    entry = SimpleNamespace(
        options={"enable_llm_api": True}, entry_id="old", title="Old"
    )
    api = llm_api._apis_for_mode(hass, entry, "http://unused", "tool_search")[0]
    fake_session(monkeypatch, tools=[tool_entry()])
    instance = await api.async_get_api_instance(SimpleNamespace())
    search = next(tool for tool in instance.tools if tool.name == "ha_search_tools")
    entry.options["enable_llm_api"] = False
    with pytest.raises(llm_api.HomeAssistantError, match="export is disabled"):
        await search.async_call(
            hass, SimpleNamespace(tool_args={"query": "state"}), SimpleNamespace()
        )


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("ha_native_llm_tools", {"api_id": "assist"}),
        ("ha_native_llm_call", {"name": "assist/change", "arguments": {}}),
    ],
)
async def test_cached_mcp_import_tool_refuses_disabled_bridge(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, arguments: dict
) -> None:
    from custom_components.ha_mcp_tools import native_server
    from ha_mcp._vendor.fastmcp import Client, FastMCP
    from ha_mcp._vendor.fastmcp.exceptions import ToolError
    from ha_mcp.runtime import Engine

    backend = SimpleNamespace(authorize=AsyncMock())
    manager = SimpleNamespace(
        entry=SimpleNamespace(
            options={"enable_native_llm_import": True, "native_import_apis": ["assist"]}
        ),
        mcp=FastMCP("Bridge opt-in"),
        backend=backend,
        engine=Engine(backend),
        hass=object(),
    )
    lookup = AsyncMock()
    monkeypatch.setattr(native_llm_import, "llm", SimpleNamespace(async_get_api=lookup))
    monkeypatch.setattr(native_server, "current_context", lambda: CallContext("alice"))
    native_llm_import.register_import_tools(manager)
    async with Client(manager.mcp) as client:
        assert tool_name in {tool.name for tool in await client.list_tools()}
        manager.entry.options["enable_native_llm_import"] = False
        with pytest.raises(ToolError):
            await client.call_tool(tool_name, arguments)
    lookup.assert_not_awaited()
