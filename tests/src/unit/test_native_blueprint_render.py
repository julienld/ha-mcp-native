"""Take control uses Core's existing renderer without a transport facade."""

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.runtime.blueprint_substitute import take_control_config
from ha_mcp.runtime.config_write_context import ConfigWriteContext
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

from .test_component_ws_search import FakeHass
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


class _BlueprintFailure(Exception):
    pass


class _UndefinedInput(Exception):
    pass


class _DomainOwner:
    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self.domain = "script"
        self.rendered = {
            "sequence": [{"delay": 1}],
            "alias": "Blueprint alias",
            "mode": "queued",
        }
        self.substitute = Mock(return_value=self.rendered)
        self.async_inputs_from_config = AsyncMock(
            return_value=SimpleNamespace(async_substitute=self.substitute)
        )


@pytest.fixture
def blueprint_owner(monkeypatch: pytest.MonkeyPatch) -> tuple:
    hass = FakeHass()
    authorize(hass)
    owner = _DomainOwner(hass)
    hass.data["blueprint"] = {"script": owner}
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.blueprint.models",
        SimpleNamespace(DomainBlueprints=_DomainOwner),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.blueprint.errors",
        SimpleNamespace(BlueprintException=_BlueprintFailure),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.util.yaml",
        SimpleNamespace(UndefinedSubstitution=_UndefinedInput),
    )
    validator = Mock(side_effect=lambda value: value)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"].config_validation,
        "path",
        validator,
        raising=False,
    )
    return hass, owner, validator


async def test_native_take_control_preserves_identity_and_uses_real_core_owner(
    native: Any, blueprint_owner: tuple
) -> None:
    hass, owner, validator = blueprint_owner
    tools = ConfigWriteContext(native.NativeBackend(hass), CallContext("actor"))
    current = {
        "id": "stable",
        "alias": "My alias",
        "description": "Keep me",
        "mode": "single",
        "use_blueprint": {"path": "author/example.yaml", "input": {"message": "Hello"}},
    }
    taken, path = await take_control_config(
        tools._render_blueprint, "script", "stable", current
    )
    assert taken == {
        "id": "stable",
        "alias": "My alias",
        "description": "Keep me",
        "sequence": [{"delay": 1}],
        "mode": "queued",
    }
    assert path == "author/example.yaml" and "use_blueprint" in current
    owner.async_inputs_from_config.assert_awaited_once_with(
        {"use_blueprint": current["use_blueprint"]}
    )
    owner.substitute.assert_called_once_with()
    validator.assert_called_once_with(path)


async def test_native_renderer_classifies_core_missing_inputs(
    native: Any, blueprint_owner: tuple
) -> None:
    hass, owner, _ = blueprint_owner
    owner.async_inputs_from_config.side_effect = _BlueprintFailure(
        "Missing input: message"
    )
    tools = ConfigWriteContext(native.NativeBackend(hass), CallContext("actor"))
    with pytest.raises(ToolFailure) as caught:
        await tools._render_blueprint("script", "author/example.yaml", {})
    assert caught.value.code == "VALIDATION_FAILED"
    owner.substitute.assert_not_called()


async def test_native_renderer_refuses_an_unregistered_domain_owner(
    native: Any, blueprint_owner: tuple
) -> None:
    hass, owner, _ = blueprint_owner
    hass.data["blueprint"]["script"] = SimpleNamespace()
    with pytest.raises(UnsupportedOperation):
        await ConfigWriteContext(
            native.NativeBackend(hass), CallContext("actor")
        )._render_blueprint("script", "author/example.yaml", {})
    owner.async_inputs_from_config.assert_not_awaited()
