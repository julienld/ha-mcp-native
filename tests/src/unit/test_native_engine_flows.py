"""Ownership and continuation tests for native Core flow ports."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from . import test_component_ws_search as _component_stubs  # noqa: F401


class UnknownFlow(Exception):
    pass


@pytest.fixture
def flows(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.flows"
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.data_entry_flow",
        SimpleNamespace(UnknownFlow=UnknownFlow),
    )
    monkeypatch.setattr(
        module,
        "_prepare_flow_result",
        lambda hass, kind, result: {
            key: value for key, value in result.items() if key != "private"
        },
    )
    return module


def manager() -> Any:
    return SimpleNamespace(
        async_init=AsyncMock(
            return_value={"flow_id": "owned", "type": "form", "step_id": "user"}
        ),
        async_configure=AsyncMock(
            return_value={
                "flow_id": "owned",
                "type": "create_entry",
                "private": "never emitted",
            }
        ),
        async_abort=Mock(),
    )


async def test_flow_uses_real_managers_and_public_serializer(flows: Any) -> None:
    owner = manager()
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(flow=owner, options=owner, subentries=owner)
    )
    authorize = AsyncMock()
    backend = flows.NativeFlows(hass, authorize)
    actor = CallContext("user", client_id="client")
    started = await backend.execute(
        "config.flow.start", {"kind": "config", "handler": "template"}, actor
    )
    owner.async_init.assert_awaited_once_with("template", context={"source": "user"})
    assert started["flow_id"] == "owned"
    result = await backend.execute(
        "config.flow.configure",
        {"flow_id": "owned", "user_input": {"name": "Example"}},
        actor,
    )
    owner.async_configure.assert_awaited_once_with("owned", {"name": "Example"})
    authorize.assert_awaited_once_with(actor)
    assert result == {"flow_id": "owned", "type": "create_entry"}
    assert not backend.owned


@pytest.mark.parametrize(
    "other",
    [CallContext("other", client_id="client"), CallContext("user", client_id="other")],
)
async def test_flow_resume_is_bound_to_actor_and_client(
    flows: Any, other: CallContext
) -> None:
    owner = manager()
    backend = flows.NativeFlows(
        SimpleNamespace(config_entries=SimpleNamespace(flow=owner)), AsyncMock()
    )
    await backend.execute(
        "config.flow.start",
        {"handler": "template"},
        CallContext("user", client_id="client"),
    )
    with pytest.raises(ToolFailure, match="does not own"):
        await backend.execute(
            "config.flow.configure", {"flow_id": "owned", "user_input": {}}, other
        )
    owner.async_configure.assert_not_awaited()


async def test_revoked_actor_cannot_resume(flows: Any) -> None:
    owner = manager()
    authorize = AsyncMock(side_effect=PermissionError("revoked"))
    backend = flows.NativeFlows(
        SimpleNamespace(config_entries=SimpleNamespace(flow=owner)), authorize
    )
    actor = CallContext("user")
    await backend.execute("config.flow.start", {"handler": "template"}, actor)
    with pytest.raises(PermissionError, match="revoked"):
        await backend.execute("config.flow.configure", {"flow_id": "owned"}, actor)
    owner.async_configure.assert_not_awaited()


async def test_flow_cap_expiry_and_stop_only_abort_owned(flows: Any) -> None:
    owner = manager()
    backend = flows.NativeFlows(
        SimpleNamespace(config_entries=SimpleNamespace(flow=owner)),
        AsyncMock(),
        max_flows=1,
        ttl=60,
    )
    actor = CallContext("user")
    await backend.execute("config.flow.start", {"handler": "template"}, actor)
    with pytest.raises(ToolFailure, match="Too many"):
        await backend.execute("config.flow.start", {"handler": "template"}, actor)
    backend.owned["owned"].touched -= 61
    await backend.execute("config.flow.start", {"handler": "template"}, actor)
    owner.async_abort.assert_called_once_with("owned")
    await backend.stop()
    assert owner.async_abort.call_count == 2 and not backend.owned


async def test_subentry_reconfigure_preserves_core_handler_and_context(
    flows: Any,
) -> None:
    owner = manager()
    backend = flows.NativeFlows(
        SimpleNamespace(config_entries=SimpleNamespace(subentries=owner)), AsyncMock()
    )
    await backend.execute(
        "config.flow.start",
        {"kind": "subentry", "handler": ["entry", "sensor"], "subentry_id": "sub"},
        CallContext("user"),
    )
    owner.async_init.assert_awaited_once_with(
        ("entry", "sensor"), context={"source": "reconfigure", "subentry_id": "sub"}
    )
