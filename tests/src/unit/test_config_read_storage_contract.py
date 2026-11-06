"""Stored automation/script/scene bodies keep their authoritative read route."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.tools import component_api
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

from .test_component_readapi_contract import _real_component_ws
from .test_component_ws_search import FakeHass
from .test_config_get_component_routing import RoutingClient as GetRoutingClient


class TestConfigGetSeam:
    """The component's ``config_get`` was withdrawn before release — it served an
    entity's ``raw_config``, whose freshness lags the config file between a write
    and the next completed reload, so a get racing a reload returned a stale
    body. All three get tools serve the body from the legacy REST pipeline
    (which reads the fresh config file). Even against a REAL component WS whose
    ``info`` advertises ``config_get``, no ``config_get`` frame is sent; the
    script and scene gets skip the caps probe entirely, and the automation get
    asks the component only for the registry side. Scenes were already
    legacy-only (a ``HomeAssistantScene`` has no raw storage body in memory);
    automation/script now share the pattern.
    """

    @pytest.mark.asyncio
    async def test_automation_get_stays_legacy_with_full_caps(self) -> None:
        stored = {
            "id": "uid-1",
            "alias": "UI Auto",
            "action": [{"service": "light.turn_on"}],
        }
        client = GetRoutingClient(automation_config=dict(stored))
        # A real component WS advertising full caps (config_get included). The
        # automation tool may probe it for the registry side, never the body.
        ws = _real_component_ws(FakeHass())
        with patch.object(
            component_api, "get_websocket_client", AsyncMock(return_value=ws)
        ):
            resp = await AutomationConfigTools(client).ha_config_get_automation(
                "uid-1", config_format="stored"
            )
        # The stored body, as Core stores it, from the legacy config fetch.
        assert resp["config"] == stored
        assert client.get_automation_config.await_count == 1
        sent = [c.args[0] for c in ws.send_command.call_args_list]
        assert "ha_mcp_tools/config_get" not in sent

    @pytest.mark.asyncio
    async def test_script_get_stays_legacy_with_full_caps(self) -> None:
        """Script analog: the legacy path returns the script BODY under
        ``config`` (category injected), the storage key staying top-level.
        The storage ``sequence`` body passes through byte-exact."""
        client = GetRoutingClient(
            script_envelope={
                "success": True,
                "script_id": "morning",
                "config": {
                    "alias": "Morning Script",
                    "sequence": [{"delay": {"seconds": 5}}],
                },
            },
            categories={"script": "cat-s"},
        )
        ws = _real_component_ws(FakeHass())
        with patch.object(
            component_api, "get_websocket_client", AsyncMock(return_value=ws)
        ):
            resp = await ConfigScriptTools(client).ha_config_get_script("morning")
        assert resp["success"] is True
        assert resp["script_id"] == "morning"
        assert resp["config"]["category"] == "cat-s"
        assert "script_id" not in resp["config"]
        # raw_config served byte-exact — the sequence body is untouched.
        assert resp["config"]["alias"] == "Morning Script"
        assert resp["config"]["sequence"] == [{"delay": {"seconds": 5}}]
        assert ws.send_command.await_count == 0
        assert client.get_script_config.await_count == 1

    @pytest.mark.asyncio
    async def test_scene_get_stays_legacy_with_full_caps(self, monkeypatch) -> None:
        """With the real component advertising ``config_get``, a scene get is
        still served entirely by the legacy path: ``ha_config_get_scene`` skips
        the caps probe, so the component WS is never touched (a
        ``HomeAssistantScene`` has no raw storage body in memory — its
        ``scene_config.states`` is runtime State objects)."""
        monkeypatch.setattr(ConfigSceneTools, "_RESOLVE_RETRY_DELAY", 0)
        client = GetRoutingClient(
            scene_envelope={
                "success": True,
                "scene_id": "movie_night",
                "config": {
                    "id": "movie_night",
                    "name": "Movie Night",
                    "entities": {"light.living_room": {"state": "on"}},
                },
            },
            categories={"scene": "cat-x"},
            registry_list=[
                {"entity_id": "scene.movie_night", "unique_id": "movie_night"}
            ],
        )
        # A real component WS advertising full caps (config_get included). The
        # scene tool must never reach for it.
        ws = _real_component_ws(FakeHass())
        with patch.object(
            component_api, "get_websocket_client", AsyncMock(return_value=ws)
        ):
            resp = await ConfigSceneTools(client).ha_config_get_scene(
                scene_id="movie_night"
            )
        assert resp["success"] is True
        assert resp["scene_id"] == "movie_night"
        # The component WS was never touched at all — no caps probe, no frame.
        assert ws.send_command.await_count == 0
        assert client.get_scene_config.await_count == 1
