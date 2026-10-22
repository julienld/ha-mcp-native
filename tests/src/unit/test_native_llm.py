"""Native Assist export/import preserves identities, owner schemas and exposure."""

from __future__ import annotations

import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import native_llm, native_llm_import  # noqa: E402
from custom_components.ha_mcp_tools.const import DOMAIN, OPT_NATIVE_IMPORT_APIS  # noqa: E402
from ha_mcp.runtime import Engine  # noqa: E402
from ha_mcp.tool_api import CallContext, ToolResult  # noqa: E402


@pytest.fixture
def schema_libraries():
    """Peers install global fake modules; this test needs the actual codecs."""
    prefixes = ("voluptuous", "voluptuous_openapi")
    saved = {name: module for name, module in sys.modules.items()
             if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)}
    for name in saved:
        sys.modules.pop(name, None)
    try:
        yield importlib.import_module("voluptuous"), importlib.import_module("voluptuous_openapi")
    finally:
        for name in list(sys.modules):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


def runtime() -> SimpleNamespace:
    backend = SimpleNamespace(authorize=AsyncMock())
    return SimpleNamespace(
        hass=object(),
        backend=backend,
        engine=Engine(backend),
        entry=SimpleNamespace(options={OPT_NATIVE_IMPORT_APIS: ["assist"]}),
    )


def context(user_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        context=SimpleNamespace(user_id=user_id), platform="test", language="fr"
    )


def test_anonymous_assist_context_never_inherits_owner() -> None:
    actor = native_llm.context_from_llm(context(None))
    assert actor.user_id is None
    assert actor.client_id == "assist:test"


async def test_export_rechecks_exposure_after_conversation_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = runtime()
    manager.engine.call = AsyncMock(return_value=ToolResult({"result": 1}))
    monkeypatch.setattr(
        native_llm, "exposed_catalog", AsyncMock(return_value=([], set()))
    )
    result = await native_llm.call_exposed(
        manager, "ha_get_state", {}, context("alice")
    )
    assert "Unknown tool" in result["error"]
    manager.engine.call.assert_not_called()


def test_import_serializes_original_voluptuous_schema_and_custom_serializer(
    schema_libraries,
) -> None:
    vol, openapi = schema_libraries

    marker = object()
    tool = SimpleNamespace(parameters=vol.Schema({vol.Required("kind"): marker}))
    instance = SimpleNamespace(
        custom_serializer=lambda value: (
            {"type": "string", "enum": ["kept"]} if value is marker else openapi.UNSUPPORTED
        )
    )
    schema = native_llm_import._schema(instance, tool)
    assert schema["properties"]["kind"]["enum"] == ["kept"]


async def test_import_refuses_unselected_and_own_exported_api() -> None:
    manager = runtime()
    for name in ("unselected", f"{DOMAIN}-entry"):
        with pytest.raises(PermissionError, match="not selected"):
            await native_llm_import.resolve_api(manager, name, CallContext("alice"))


async def test_import_uses_owner_validator_and_preserves_error_result(
    monkeypatch: pytest.MonkeyPatch, schema_libraries,
) -> None:
    vol, _openapi = schema_libraries
    manager = runtime()
    validator = vol.Schema({vol.Required("number"): vol.Coerce(int)})
    tool = SimpleNamespace(
        name="native",
        integration="fixture",
        parameters=validator,
        annotations=SimpleNamespace(read_only=True),
    )
    instance = SimpleNamespace(
        tools=[tool],
        async_call_tool=AsyncMock(
            return_value=SimpleNamespace(data={"value": 4}, error=True)
        ),
    )
    monkeypatch.setattr(
        native_llm_import, "resolve_api", AsyncMock(return_value=instance)
    )
    monkeypatch.setattr(native_llm_import.llm, "ToolInput", SimpleNamespace)
    monkeypatch.setattr(
        native_llm_import, "json_bytes", lambda value: json.dumps(value).encode()
    )
    result = await native_llm_import.call_imported(
        manager, "assist/native", {"number": "4"}, CallContext("alice")
    )
    assert result.data == {"value": 4} and result.is_error
    call = instance.async_call_tool.await_args.args[0]
    assert call.tool_args == {"number": 4} and call.external is True


async def test_import_rechecks_owner_inside_owned_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = runtime()
    tool = SimpleNamespace(
        name="native",
        integration="original",
        annotations=SimpleNamespace(read_only=False),
    )
    swapped = SimpleNamespace(
        name="native",
        integration="replacement",
        annotations=SimpleNamespace(read_only=False),
    )
    second = SimpleNamespace(tools=[swapped], async_call_tool=AsyncMock())
    monkeypatch.setattr(
        native_llm_import,
        "resolve_api",
        AsyncMock(side_effect=[SimpleNamespace(tools=[tool]), second]),
    )
    result = await native_llm_import.call_imported(
        manager, "assist/native", {}, CallContext("alice")
    )
    assert result.is_error
    second.async_call_tool.assert_not_called()


async def test_import_cannot_recurse_into_same_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = runtime()
    stack = native_llm_import._IMPORT_STACK.set(("assist",))
    try:
        with pytest.raises(PermissionError, match="import loop"):
            await native_llm_import.resolve_api(manager, "assist", CallContext("alice"))
    finally:
        native_llm_import._IMPORT_STACK.reset(stack)
