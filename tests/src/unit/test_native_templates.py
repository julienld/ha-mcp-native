"""Templates and conditions use Core owners, preserving errors and cleanup."""

import asyncio
import importlib
import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext

from .test_component_helper_collections import FakeCollection, _hass
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


@pytest.fixture
async def rendering(native: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    hass = _hass(FakeCollection())
    hass.loop = asyncio.get_running_loop()
    user = authorize(hass)
    state = SimpleNamespace(
        timeout=False,
        render_error=None,
        value=42,
        warning=None,
        listeners={
            "all": False,
            "domains": set(),
            "entities": {"sensor.example"},
            "time": False,
        },
        remove=Mock(),
        track_calls=[],
        guards=[],
        normalized=[],
    )

    class TemplateError(Exception):
        pass

    class Template:
        def __init__(self, text: str, instance: Any) -> None:
            assert instance is hass
            self.text = text

        async def async_render_will_timeout(
            self, timeout: float, variables: Any, **kwargs: Any
        ) -> bool:
            state.guards.append((timeout, variables, kwargs["strict"]))
            if state.warning and kwargs["log_fn"]:
                kwargs["log_fn"](30, state.warning)
            if state.render_error:
                raise state.render_error
            return state.timeout

    def track(instance: Any, templates: list, listener: Any, **kwargs: Any) -> Any:
        assert instance is hass
        state.track_calls.append((templates, kwargs))
        return SimpleNamespace(
            listeners=state.listeners,
            async_refresh=lambda: listener(None, [SimpleNamespace(result=state.value)]),
            async_remove=state.remove,
        )

    def schema(message: dict) -> dict:
        state.normalized.append(message)
        return {
            **message,
            **({"timeout": float(message["timeout"])} if "timeout" in message else {}),
        }

    handlers = {
        key: (Mock(side_effect=AssertionError("A WS handler was called")), schema)
        for key in ("render_template", "test_condition")
    }
    hass.data["websocket_api"].update(handlers)
    helpers = sys.modules["homeassistant.helpers"]
    monkeypatch.setattr(
        sys.modules["homeassistant.exceptions"],
        "TemplateError",
        TemplateError,
        raising=False,
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.template",
        SimpleNamespace(Template=Template),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.event",
        SimpleNamespace(
            TrackTemplate=lambda *args: args, async_track_template_result=track
        ),
    )
    condition = SimpleNamespace(
        async_check=Mock(return_value=False), async_unload=Mock()
    )
    conditions = SimpleNamespace(
        async_validate_condition_config=AsyncMock(
            return_value={"checked_by_core": True}
        ),
        async_from_config=AsyncMock(return_value=condition),
    )
    monkeypatch.setattr(helpers, "condition", conditions, raising=False)
    monkeypatch.setattr(
        helpers,
        "trace",
        SimpleNamespace(
            trace_get=lambda: {
                "check": [SimpleNamespace(template_errors=["Core condition warning"])]
            },
            suppress_template_error_logging=nullcontext,
        ),
        raising=False,
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.templates"
    )
    monkeypatch.setattr(
        module, "json_bytes", lambda value: json.dumps(value, default=list).encode()
    )
    monkeypatch.setattr(module, "callback", lambda function: function)
    backend = native.NativeBackend(hass)
    engine = Engine(backend, [module.template_definition(backend)])
    return SimpleNamespace(
        engine=engine,
        hass=hass,
        user=user,
        state=state,
        handlers=handlers,
        condition=condition,
        conditions=conditions,
        template=Template,
        error=TemplateError,
    )


async def test_core_render_keeps_warnings_dependencies_and_exact_arguments(
    rendering: Any,
) -> None:
    rendering.state.warning = "A Core undefined-variable warning"
    result = await rendering.engine.call(
        "ha_eval_template",
        {"template": "{{ value }}", "variables": {"value": 42}, "strict": True},
        CallContext("actor", read_only=True),
    )
    assert not result.is_error, result.data
    assert result.data["result"] == 42
    assert result.data["warnings"] == [rendering.state.warning]
    assert result.data["listeners"]["entities"] == ["sensor.example"]
    assert rendering.state.guards == [(3.0, {"value": 42}, True)]
    assert rendering.state.track_calls[0][1]["strict"] is True
    rendering.state.remove.assert_called_once()
    assert not rendering.engine.backend._templates.cleanups
    for handler, _ in rendering.handlers.values():
        handler.assert_not_called()


async def test_core_timeout_does_not_rerender_or_create_a_tracker(
    rendering: Any,
) -> None:
    rendering.state.timeout = True
    result = await rendering.engine.call(
        "ha_eval_template",
        {"template": "{{ slow }}", "timeout": 1},
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "TIMEOUT_OPERATION"
    assert rendering.state.guards == [(1.0, None, False)]
    assert not rendering.state.track_calls
    rendering.state.remove.assert_not_called()


async def test_core_failure_keeps_line_without_a_second_diagnostic_render(
    rendering: Any,
) -> None:
    cause = ValueError("UndefinedError: missing")
    cause.lineno = 2
    error = rendering.error("UndefinedError: missing")
    error.__cause__ = cause
    rendering.state.render_error = error
    result = await rendering.engine.call(
        "ha_eval_template",
        {"template": "first\n{{ missing }}", "report_errors": False},
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "VALIDATION_FAILED"
    assert result.data["line"] == 2 and result.data["source_line"] == "{{ missing }}"
    assert len(rendering.state.guards) == 1 and not rendering.state.track_calls


async def test_condition_delegates_normalization_validation_trace_and_unload(
    rendering: Any,
) -> None:
    supplied = {"condition": "state", "entity_id": "sensor.example", "state": "on"}
    result = await rendering.engine.call(
        "ha_eval_template",
        {"condition": supplied, "variables": {"sample": True}},
        CallContext("actor", read_only=True),
    )
    assert not result.is_error and result.data["result"] is False
    assert result.data["condition"] == supplied
    assert result.data["warnings"] == ["Core condition warning"]
    rendering.conditions.async_validate_condition_config.assert_awaited_once_with(
        rendering.hass, supplied
    )
    rendering.conditions.async_from_config.assert_awaited_once_with(
        rendering.hass, {"checked_by_core": True}
    )
    rendering.condition.async_check.assert_called_once_with(variables={"sample": True})
    rendering.condition.async_unload.assert_called_once()


async def test_actor_revoked_during_condition_setup_is_not_evaluated_and_is_cleaned(
    rendering: Any,
) -> None:
    async def create(*_args: Any) -> Any:
        rendering.user.is_active = False
        return rendering.condition

    rendering.conditions.async_from_config.side_effect = create
    result = await rendering.engine.call(
        "ha_eval_template", {"condition": {"condition": "state"}}, CallContext("actor")
    )
    assert result.is_error
    rendering.condition.async_check.assert_not_called()
    rendering.condition.async_unload.assert_called_once()


async def test_failed_tracker_cleanup_is_retained_for_stop_retry(
    rendering: Any,
) -> None:
    rendering.state.remove.side_effect = RuntimeError("Cleanup failed")
    result = await rendering.engine.call(
        "ha_eval_template", {"template": "{{ value }}"}, CallContext("actor")
    )
    assert result.is_error and rendering.engine.backend._templates.cleanups
    with pytest.raises(RuntimeError, match="native_resources_not_closed"):
        await rendering.engine.backend.stop()
    rendering.state.remove.side_effect = None
    await rendering.engine.backend.stop()
    assert not rendering.engine.backend._templates.cleanups


async def test_cancellation_drains_core_guard_and_never_starts_a_late_tracker(
    rendering: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def guarded(*_args: Any, **_kwargs: Any) -> bool:
        entered.set()
        await release.wait()
        return False

    monkeypatch.setattr(rendering.template, "async_render_will_timeout", guarded)
    owner = rendering.engine.backend._templates
    task = asyncio.create_task(
        owner.evaluate(CallContext("actor"), "{{ test }}", None, None, False, 3, True)
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not rendering.state.track_calls and not owner.cleanups
