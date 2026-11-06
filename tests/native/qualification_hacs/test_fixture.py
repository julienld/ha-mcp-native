"""Control-endpoint regressions; Core/owner execution remains a runner check."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web


@pytest.mark.asyncio
async def test_first_dummy_load_refreshes_cores_pre_install_component_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Core caches discovery before the dummy exists. HACS only clears that
    # catalog for config-flow integrations; this fixture has YAML setup.
    hass = SimpleNamespace(data={"custom_components": {"hacs": object()}})

    async def discover(_hass: Any) -> dict[str, Any]:
        return hass.data.setdefault(
            "custom_components", {"qualification_dummy": object()}
        )

    async def setup(_hass: Any, domain: str, _config: dict[str, Any]) -> bool:
        assert domain in await discover(hass), "stale Core custom-component catalog"
        return True

    setup_component = AsyncMock(side_effect=setup)
    for name, module in {
        "homeassistant.components.http": SimpleNamespace(
            HomeAssistantView=type(
                "View", (), {"json": staticmethod(web.json_response)}
            )
        ),
        "homeassistant.core": SimpleNamespace(
            Event=object, HomeAssistant=object, callback=lambda fn: fn
        ),
        "homeassistant.helpers": SimpleNamespace(entity_registry=object()),
        "homeassistant.helpers.start": SimpleNamespace(async_at_started=object()),
        "homeassistant.loader": SimpleNamespace(
            DATA_CUSTOM_COMPONENTS="custom_components",
            async_get_custom_components=discover,
        ),
        "homeassistant.setup": SimpleNamespace(async_setup_component=setup_component),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    package = "hacs_qualification_fixture_test"
    root = Path(__file__).parent / "fixture"
    spec = importlib.util.spec_from_file_location(
        package, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    assert spec and spec.loader
    fixture = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package, fixture)
    # Restore the relative content-module import too, even on an assertion failure.
    monkeypatch.setitem(sys.modules, package + ".content", None)
    del sys.modules[package + ".content"]
    spec.loader.exec_module(fixture)
    hass.data[fixture.DOMAIN] = {"network": None}

    class Request(dict):
        async def json(self) -> dict[str, Any]:
            return {}

    response = await fixture.QualificationView(hass).post(
        Request(hass_user=SimpleNamespace(is_admin=True)), "load_dummy"
    )
    assert response.status == 200
    setup_component.assert_awaited_once_with(hass, "qualification_dummy", {})
