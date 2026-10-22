"""Native Assist export/import preserves identities, owner schemas and exposure."""

from __future__ import annotations

import asyncio
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
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolResult  # noqa: E402


@pytest.fixture
def schema_libraries():
    """Peers install global fake modules; this test needs the actual codecs."""
    prefixes = ("voluptuous", "voluptuous_openapi")
    saved = {
        name: module
        for name, module in sys.modules.items()
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)
    }
    for name in saved:
        sys.modules.pop(name, None)
    try:
        yield (
            importlib.import_module("voluptuous"),
            importlib.import_module("voluptuous_openapi"),
        )
    finally:
        for name in list(sys.modules):
            if any(
                name == prefix or name.startswith(prefix + ".") for prefix in prefixes
            ):
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


async def test_export_preserves_legacy_result_envelope_and_operation_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = runtime()
    manager.engine.call = AsyncMock(
        return_value=ToolResult(
            {"changed": True}, status="confirmed", operation_id="owned-operation"
        )
    )
    monkeypatch.setattr(
        native_llm,
        "exposed_catalog",
        AsyncMock(return_value=([SimpleNamespace(name="ha_call_service")], set())),
    )
    result = await native_llm.call_exposed(
        manager, "ha_call_service", {}, context("alice")
    )
    data = result if isinstance(result, dict) else result.data
    assert data["structuredContent"] == {"changed": True}
    assert data["meta"]["ha_mcp"] == {
        "status": "confirmed",
        "operation_id": "owned-operation",
    }


async def test_assist_alias_is_adapted_before_exposure_and_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = runtime()
    manager.engine.call = AsyncMock(
        return_value=ToolResult({"blueprints": []}, status="confirmed")
    )
    monkeypatch.setattr(
        native_llm,
        "exposed_catalog",
        AsyncMock(return_value=([SimpleNamespace(name="ha_manage_blueprints")], set())),
    )
    await native_llm.call_exposed(manager, "ha_get_blueprint", {}, context("alice"))
    assert manager.engine.call.await_args.args[:2] == (
        "ha_manage_blueprints",
        {"action": "list"},
    )


def test_import_serializes_original_voluptuous_schema_and_custom_serializer(
    schema_libraries,
) -> None:
    vol, openapi = schema_libraries

    marker = object()
    tool = SimpleNamespace(parameters=vol.Schema({vol.Required("kind"): marker}))
    instance = SimpleNamespace(
        custom_serializer=lambda value: (
            {"type": "string", "enum": ["kept"]}
            if value is marker
            else openapi.UNSUPPORTED
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
    monkeypatch: pytest.MonkeyPatch,
    schema_libraries,
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
        api=SimpleNamespace(id="assist"),
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
        native_llm_import.llm,
        "async_get_apis",
        lambda _hass: [instance.api],
        raising=False,
    )
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
    api = SimpleNamespace(id="assist")
    second = SimpleNamespace(api=api, tools=[swapped], async_call_tool=AsyncMock())
    monkeypatch.setattr(
        native_llm_import,
        "resolve_api",
        AsyncMock(side_effect=[SimpleNamespace(api=api, tools=[tool]), second]),
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


async def test_import_failure_after_dispatch_preserves_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = runtime()
    instance = SimpleNamespace(
        api=SimpleNamespace(id="assist"),
        tools=[
            SimpleNamespace(
                name="write",
                integration="fixture",
                parameters=dict,
                annotations=SimpleNamespace(read_only=False),
            )
        ],
        async_call_tool=AsyncMock(side_effect=RuntimeError("Failed after effect")),
    )
    monkeypatch.setattr(
        native_llm_import, "resolve_api", AsyncMock(return_value=instance)
    )
    monkeypatch.setattr(native_llm_import.llm, "ToolInput", SimpleNamespace)
    monkeypatch.setattr(
        native_llm_import.llm,
        "async_get_apis",
        lambda _hass: [instance.api],
        raising=False,
    )
    result = await native_llm_import.call_imported(
        manager, "assist/write", {}, CallContext("alice")
    )
    instance.async_call_tool.assert_awaited_once()
    assert result.is_error and result.status == "outcome_unknown"


@pytest.mark.parametrize("suspension", ["authorization", "approval"])
async def test_assist_exposure_removed_during_admission_cannot_execute(
    monkeypatch: pytest.MonkeyPatch, suspension: str
) -> None:
    manager = runtime()
    manager.policy = SimpleNamespace(llm_overrides={})
    handler = AsyncMock(return_value={"changed": True})
    manager.engine.register(ToolDefinition("write", "Write", {}, handler))
    monkeypatch.setattr(
        native_llm,
        "exposed_catalog",
        AsyncMock(return_value=([SimpleNamespace(name="write")], set())),
    )
    suspended, resume = asyncio.Event(), asyncio.Event()
    authorizations = 0

    async def authorize(_actor: CallContext) -> None:
        nonlocal authorizations
        authorizations += 1
        if suspension == "authorization" and authorizations == 2:
            suspended.set()
            await resume.wait()

    async def approve(*_args: object) -> None:
        if suspension == "approval":
            suspended.set()
            await resume.wait()

    manager.backend.authorize = authorize
    manager.engine.policy_guard = approve
    task = asyncio.create_task(
        native_llm.call_exposed(manager, "write", {}, context("alice"))
    )
    await asyncio.wait_for(suspended.wait(), 1)
    manager.policy.llm_overrides["write"] = False
    resume.set()
    result = await asyncio.wait_for(task, 1)
    handler.assert_not_awaited()
    data = result if isinstance(result, dict) else result.data
    assert data["isError"]


async def test_assist_nested_calls_recheck_child_exposure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = runtime()
    manager.policy = SimpleNamespace(llm_overrides={})
    child = AsyncMock(return_value={"changed": True})

    async def outer(_arguments: dict, actor: CallContext) -> ToolResult:
        manager.policy.llm_overrides["child"] = False
        return await manager.engine.call("child", {}, actor)

    manager.engine.register(ToolDefinition("outer", "Outer", {}, outer))
    manager.engine.register(ToolDefinition("child", "Child", {}, child))
    monkeypatch.setattr(
        native_llm,
        "exposed_catalog",
        AsyncMock(
            return_value=(
                [SimpleNamespace(name="outer"), SimpleNamespace(name="child")],
                set(),
            )
        ),
    )
    await native_llm.call_exposed(manager, "outer", {}, context("alice"))
    child.assert_not_awaited()
