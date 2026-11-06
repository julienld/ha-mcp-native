"""The real native fixture must load and keep validation on each Core API shape."""

import importlib
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(params=["voluptuous", "probatio"])
def fixture_core(request: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    # Other component suites install incomplete validation facades at collection.
    # Use the real validator here, then restore their whole module namespace.
    package = request.param
    original = {
        name: module
        for name, module in sys.modules.items()
        if name == package or name.startswith(package + ".")
    }

    def restore_validation() -> None:
        for name in list(sys.modules):
            if name == package or name.startswith(package + "."):
                sys.modules.pop(name)
        sys.modules.update(original)

    request.addfinalizer(restore_validation)
    for name in original:
        sys.modules.pop(name)
    validation = importlib.import_module(request.param)
    llm = ModuleType("homeassistant.helpers.llm")
    llm.Tool = type("Tool", (), {"parameters": validation.Schema({})})
    llm.API = type("API", (), {})
    llm.ToolInput = SimpleNamespace
    llm.LLMContext = SimpleNamespace
    if request.param == "probatio":
        llm.ToolResult = lambda **values: SimpleNamespace(error=False, **values)
        llm.ToolAnnotations = SimpleNamespace
    else:
        # Old Core must never need the new schema dependency merely to boot.
        monkeypatch.setitem(sys.modules, "probatio", None)
    modules = {
        "aiohttp": SimpleNamespace(
            web=SimpleNamespace(json_response=lambda data: data)
        ),
        "homeassistant": ModuleType("homeassistant"),
        "homeassistant.auth": ModuleType("homeassistant.auth"),
        "homeassistant.auth.const": SimpleNamespace(
            GROUP_ID_ADMIN="admin", GROUP_ID_USER="user"
        ),
        "homeassistant.components": ModuleType("homeassistant.components"),
        "homeassistant.components.http": SimpleNamespace(
            HomeAssistantView=type("HomeAssistantView", (), {})
        ),
        "homeassistant.const": SimpleNamespace(
            Platform=SimpleNamespace(SENSOR="sensor")
        ),
        "homeassistant.core": SimpleNamespace(Context=SimpleNamespace),
        "homeassistant.helpers": SimpleNamespace(llm=llm),
        "homeassistant.helpers.llm": llm,
        "homeassistant.setup": SimpleNamespace(async_setup_component=AsyncMock()),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    name = "_native_fixture_compat_test"
    path = Path(__file__).parents[2] / "native/fixtures/native_fixture/__init__.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    try:
        spec.loader.exec_module(module)
        yield SimpleNamespace(module=module, llm=llm, validation=validation)
    finally:
        for child in list(sys.modules):
            if child.startswith(name + "."):
                sys.modules.pop(child)


async def test_fixture_echo_loads_without_new_core_dependencies_and_validates_its_input(
    fixture_core: Any,
) -> None:
    tool = fixture_core.module.EchoTool()
    context = SimpleNamespace(context=SimpleNamespace(user_id="original-actor"))
    result = await tool.async_call(
        None, SimpleNamespace(tool_args={"text": "native"}), context
    )
    data = result if isinstance(result, dict) else result.data
    assert data == {"text": "NATIVE", "user_id": "original-actor"}
    assert isinstance(result, dict) == (not hasattr(fixture_core.llm, "ToolResult"))
    for invalid in ({"text": 1}, {"text": "native", "extra": True}, {}):
        with pytest.raises(fixture_core.validation.Invalid):
            await tool.async_call(None, SimpleNamespace(tool_args=invalid), context)


async def test_fixture_action_keeps_service_validation_and_the_original_actor(
    fixture_core: Any,
) -> None:
    tool = fixture_core.module.TurnOnTool()
    call = AsyncMock()
    hass = SimpleNamespace(services=SimpleNamespace(async_call=call))
    context = SimpleNamespace(context=SimpleNamespace(user_id="original-actor"))
    await tool.async_call(hass, SimpleNamespace(tool_args={}), context)
    call.assert_awaited_once_with(
        "input_boolean",
        "turn_on",
        {"entity_id": "input_boolean.native_light"},
        blocking=True,
        context=context.context,
    )
    call.reset_mock()
    with pytest.raises(fixture_core.validation.Invalid):
        await tool.async_call(hass, SimpleNamespace(tool_args={"extra": True}), context)
    call.assert_not_awaited()


async def test_fixture_observes_assist_data_and_failures_with_both_core_result_shapes(
    fixture_core: Any,
) -> None:
    payload = {"error": {"code": "IMAGE_CONTENT_UNSUPPORTED"}}
    result_type = getattr(fixture_core.llm, "ToolResult", None)
    result = (
        payload if result_type is None else SimpleNamespace(data=payload, error=True)
    )
    fixture_core.llm.async_get_api = AsyncMock(
        return_value=SimpleNamespace(async_call_tool=AsyncMock(return_value=result))
    )
    view = fixture_core.module.FixtureView(object())
    response = await view._llm(
        SimpleNamespace(id="actor"),
        SimpleNamespace(entry=SimpleNamespace(entry_id="entry")),
        {"tool_name": "ha_get_camera_image"},
    )
    assert response == {"data": payload, "error": True}


async def test_dependency_receipt_keeps_the_requested_lifecycle_phase(
    fixture_core: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def observe(component: Path, *, phase: str = "unspecified") -> dict[str, Any]:
        return {"component": str(component), "phase": phase}

    monkeypatch.setitem(
        sys.modules,
        fixture_core.module.__name__ + ".dependency_receipt",
        SimpleNamespace(observe_dependencies=observe),
    )

    async def executor(job: Any, *args: Any) -> Any:
        return job(*args)

    component = tmp_path / "custom_components/ha_mcp_tools"
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda _: str(component)),
        async_add_executor_job=executor,
    )
    response = await fixture_core.module.FixtureView(hass)._observation(
        SimpleNamespace(query={"phase": "after_restart"}), "dependency_receipt"
    )
    assert response == {"component": str(component), "phase": "after_restart"}
