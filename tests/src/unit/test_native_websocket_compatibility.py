"""Shared refusal policies precede the explicit native raw-transport boundary."""

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import build_initial_catalog
from ha_mcp.tool_api import CallContext

from .test_component_ws_phase2_async import _call_hass, _FakeCallServices
from .test_native_engine_backend import EventBus, authorize
from .test_native_engine_backend import native as native


@pytest.mark.parametrize(
    ("arguments", "code", "message"),
    [
        (
            {"ws_command": "repairs/list_issues"},
            "UNSUPPORTED_OPERATION",
            "does not implement",
        ),
        (
            {"ws_command": "subscribe_events"},
            "VALIDATION_FAILED",
            "streaming or two-phase",
        ),
        (
            {"ws_command": "ha_mcp_tools/info"},
            "VALIDATION_FAILED",
            "ha_mcp_tools",
        ),
        (
            {"ws_command": "repairs/list_issues", "domain": "light"},
            "VALIDATION_FAILED",
            "not both",
        ),
        (
            {"ws_command": "call_service"},
            "VALIDATION_FAILED",
            "invokes Home Assistant services",
        ),
        (
            {"ws_command": "config/core/update"},
            "VALIDATION_FAILED",
            "dedicated tool guards",
        ),
        (
            {"ws_command": "repairs/list_issues", "data": {"id": 5}},
            "VALIDATION_FAILED",
            "reserved",
        ),
    ],
)
async def test_native_raw_command_boundary_never_dispatches_a_core_service(
    native: Any, arguments: dict[str, Any], code: str, message: str
) -> None:
    services = _FakeCallServices(known=set())
    hass = _call_hass([], services, EventBus())
    authorize(hass)
    backend = native.NativeBackend(hass)
    engine = Engine(backend, build_initial_catalog(backend))
    result = await engine.call("ha_call_service", arguments, CallContext("actor"))
    assert result.is_error and result.data["error"]["code"] == code
    assert message in result.data["error"]["message"]
    assert result.status == "not_started"
    assert services.calls == []


@pytest.mark.parametrize("native_selected", [True, False])
@pytest.mark.parametrize("legacy_contract", [True, False])
def test_transport_marker_preserves_all_shared_and_legacy_coverage(
    native_selected: bool, legacy_contract: bool
) -> None:
    source = Path(__file__).parents[1] / "e2e" / "_conftest_collection.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_apply_legacy_runtime_only_skip"
    )
    scope: dict[str, Any] = {"Any": Any, "pytest": pytest}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"),
        scope,
    )
    markers: list[Any] = []
    item = SimpleNamespace(
        keywords={"legacy_runtime_only"} if legacy_contract else set(),
        add_marker=markers.append,
    )
    scope[function.name](item, native_selected)
    assert bool(markers) == (native_selected and legacy_contract)
