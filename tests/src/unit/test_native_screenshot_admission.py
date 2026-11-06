"""Rendering may persist a profile and needs live admission at HTTP dispatch."""

import httpx
import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.mutation_guard import MutationAdmission, current_admission
from ha_mcp.tool_api import ToolFailure

from .test_native_engine_backend import native as native
from .test_native_screenshots import renderer as renderer


@pytest.mark.parametrize("revoke_after_observation", [False, True])
async def test_capture_cannot_bypass_read_only_or_changed_policy(
    renderer, monkeypatch, revoke_after_observation
):
    renderer, requests, *_ = renderer
    allowed = True

    def check_policy():
        if not allowed:
            raise ToolFailure("POLICY_DENIED", "Capture was disabled")

    if revoke_after_observation:
        from custom_components.ha_mcp_tools.native_operations.screenshot_theme import (
            NativeThemeObserver,
        )

        async def observe(_self):
            nonlocal allowed
            allowed = False

        monkeypatch.setattr(NativeThemeObserver, "take_snapshot", observe)
    token = current_admission.set(
        MutationAdmission(renderer.actor, revoke_after_observation, check_policy)
    )
    try:
        with pytest.raises(ToolFailure) as error:
            await renderer.capture(
                {}, "wall-panel", {"views": []}, None, after_write=False
            )
        assert error.value.code == (
            "POLICY_DENIED" if revoke_after_observation else "READ_ONLY"
        )
        assert requests == []
    finally:
        current_admission.reset(token)


async def test_standalone_capture_returns_images_with_the_existing_arguments(renderer):
    from custom_components.ha_mcp_tools.native_screenshot_tools import (
        screenshot_definition,
    )

    renderer, requests, _status, _active, png = renderer
    engine = Engine(renderer.manager.backend, [screenshot_definition(renderer.manager)])
    result = await engine.call(
        "ha_get_dashboard_screenshot",
        {"url_path": "default", "viewport_presets": ["mobile", "desktop"]},
        renderer.actor,
    )
    assert not result.is_error and len(result.images) == 2, result
    assert result.images[0].data == png and result.data["screenshot_count"] == 2
    assert [request.url.params["viewport"] for request in requests] == [
        "390x844",
        "1280x800",
    ]
    assert result.data["render_path"] == "lovelace"


@pytest.mark.parametrize("full_page,status", [(True, 400), (False, 500)])
async def test_uncertain_render_is_not_retried_and_stops_other_viewports(
    renderer, full_page, status
):
    from custom_components.ha_mcp_tools.native_screenshot_tools import (
        screenshot_definition,
    )

    renderer, requests, statuses, *_ = renderer
    statuses[0] = status
    engine = Engine(renderer.manager.backend, [screenshot_definition(renderer.manager)])
    result = await engine.call(
        "ha_get_dashboard_screenshot",
        {
            "url_path": "default",
            "full_page": full_page,
            "viewport_presets": ["mobile", "desktop"],
        },
        renderer.actor,
    )
    assert result.is_error and result.status == "outcome_unknown", result
    assert len(requests) == 1 and result.images == ()
    assert "No retry" in result.data["error"]["message"]


async def test_partial_render_keeps_known_images_without_dispatching_the_tail(renderer):
    from custom_components.ha_mcp_tools.native_screenshot_tools import (
        screenshot_definition,
    )

    renderer, requests, statuses, _active, png = renderer

    class FirstImage(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield png

        async def aclose(self):
            statuses[0] = 500

    statuses.append(FirstImage())
    engine = Engine(renderer.manager.backend, [screenshot_definition(renderer.manager)])
    result = await engine.call(
        "ha_get_dashboard_screenshot",
        {"url_path": "default", "viewport_presets": ["mobile", "tablet", "desktop"]},
        renderer.actor,
    )
    assert not result.is_error and result.status == "outcome_unknown", result
    assert result.data["partial"] and result.data["screenshot_count"] == 1
    assert len(result.images) == 1 and result.images[0].data == png
    assert len(requests) == 2 and result.data["retry_performed"] is False


@pytest.mark.parametrize("gate", ["visibility", "feature"])
async def test_capture_rechecks_visibility_and_features_after_the_theme_observation(
    renderer, monkeypatch, gate
):
    from dataclasses import replace
    from types import SimpleNamespace

    from custom_components.ha_mcp_tools.native_operations.screenshot_theme import (
        NativeThemeObserver,
    )

    renderer, requests, *_ = renderer
    hidden = False

    async def before(*_args):
        if hidden and gate == "visibility":
            raise ToolFailure("ENTITY_VISIBILITY_ENFORCED", "Images are opaque")

    async def observe(_self):
        nonlocal hidden
        hidden = True
        if gate == "feature":
            renderer.manager.policy = replace(
                renderer.manager.policy,
                flags={
                    **renderer.manager.policy.flags,
                    "enable_dashboard_screenshot": False,
                },
            )

    renderer.manager.visibility = SimpleNamespace(before=before)
    monkeypatch.setattr(NativeThemeObserver, "take_snapshot", observe)
    with pytest.raises(ToolFailure) as error:
        await renderer.capture({}, "wall-panel", {"views": []}, None, after_write=False)
    assert (
        error.value.code
        == (
            "ENTITY_VISIBILITY_ENFORCED" if gate == "visibility" else "FEATURE_DISABLED"
        )
        and requests == []
    )


async def test_revocation_after_a_render_never_reports_no_execution(
    renderer, monkeypatch
):
    from custom_components.ha_mcp_tools.native_operations.screenshot_theme import (
        NativeThemeObserver,
    )

    renderer, requests, _status, active, _png = renderer

    async def revoke(_self):
        active[0] = False

    monkeypatch.setattr(NativeThemeObserver, "detect_change", revoke)
    with pytest.raises(ToolFailure) as error:
        await renderer.capture({}, "wall-panel", {"views": []}, None, after_write=False)
    assert error.value.code == "AUTH_FORBIDDEN" and len(requests) == 1
    assert error.value.status == "outcome_unknown"
