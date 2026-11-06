"""Optional integrations must remain inactive until Core has loaded their owner."""

import importlib
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.tool_api import ToolFailure

from .test_native_engine_backend import native as native


async def test_unloaded_knx_never_reads_a_leftover_owner(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.integration_inspection"
    )
    project = SimpleNamespace(get_knxproject=AsyncMock(return_value={"stale": True}))
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.knx.const",
        SimpleNamespace(KNX_MODULE_KEY="knx"),
    )
    hass = SimpleNamespace(
        config=SimpleNamespace(components=set()),
        data={"knx": SimpleNamespace(project=project)},
    )

    result = await module.knx_project(hass, {})

    assert result["success"] is False
    project.get_knxproject.assert_not_awaited()


@pytest.fixture
def resolver(native: Any) -> ModuleType:
    return importlib.import_module(
        "custom_components.ha_mcp_tools.optional_integrations"
    )


def test_configured_but_unloaded_integration_never_imports_packages(
    resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    load = Mock(side_effect=AssertionError("Must not import an inactive integration"))
    monkeypatch.setattr(resolver, "import_module", load)
    hass = SimpleNamespace(
        config=SimpleNamespace(components=set()),
        data={"matter": object()},
    )

    with pytest.raises(ToolFailure, match="not loaded"):
        resolver.loaded_integration_module(hass, "matter.api")

    load.assert_not_called()


def test_loaded_integration_uses_its_real_api(
    resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = object()
    load = Mock(return_value=owner)
    monkeypatch.setattr(resolver, "import_module", load)
    hass = SimpleNamespace(config=SimpleNamespace(components={"matter"}))

    assert resolver.loaded_integration_module(hass, "matter.api") is owner
    load.assert_called_once_with("homeassistant.components.matter.api")


def test_broken_installed_dependency_is_not_hidden_as_unavailable(
    resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass = SimpleNamespace(config=SimpleNamespace(components={"matter"}))
    load = Mock(side_effect=ModuleNotFoundError("broken installed dependency"))
    monkeypatch.setattr(resolver, "import_module", load)
    with pytest.raises(ModuleNotFoundError, match="broken installed dependency"):
        resolver.loaded_integration_module(hass, "matter.api")


def test_unload_during_resolution_does_not_return_an_obsolete_api(
    resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass = SimpleNamespace(config=SimpleNamespace(components={"matter"}))

    def unload(module: str) -> object:
        hass.config.components.clear()
        return object()

    monkeypatch.setattr(resolver, "import_module", unload)
    with pytest.raises(ToolFailure, match="unloaded during API resolution"):
        resolver.loaded_integration_module(hass, "matter.api")
